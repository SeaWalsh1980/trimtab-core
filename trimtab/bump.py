"""Moving a project's lock: which items changed, and which overrides must be re-confirmed.

`/trimtab-bump` opens a draft PR moving `trimtab_sha` forward (plan section 3).
The PR lists every base item that changed between the locked SHA and the new
one, and flags each override citing a changed ID: "re-confirm or drop". The
lock is written only behind a confirmation gate: `plan` reads, `apply` needs the
dry run's token and refuses when the lock has moved since.

The lock file is rewritten by replacing the one value, so its formatting and
key order survive.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from trimtab import config as project_config
from trimtab.items import Item
from trimtab.lint.overrides import base_items_at, parse_entries
from trimtab.registry.rules import OVERRIDES

SHA_VALUE = re.compile(r'("trimtab_sha"\s*:\s*")([^"]*)(")')


class BumpError(Exception):
    """The bump cannot be planned or applied as asked."""


@dataclass(frozen=True)
class Change:
    id: str
    what: str  # added | removed | text | strength | withdrawn
    detail: str = ""


@dataclass(frozen=True)
class BumpPlan:
    old: str
    new: str
    changes: tuple[Change, ...]
    flagged: tuple[tuple[str, int], ...]  # (item ID, overrides.md entry number)


def changes(before: Mapping[str, Item], after: Mapping[str, Item]) -> list[Change]:
    """Every item that differs between two registries, in ID order. Pure."""
    out = []
    for item_id in sorted(set(before) | set(after)):
        a, b = before.get(item_id), after.get(item_id)
        if a is None:
            out.append(Change(item_id, "added", b.strength.label))
        elif b is None:
            out.append(Change(item_id, "removed", "no withdrawal stub"))
        elif b.withdrawn and not a.withdrawn:
            out.append(Change(item_id, "withdrawn"))
        elif a.strength != b.strength:
            out.append(Change(item_id, "strength", f"{a.strength.label} -> {b.strength.label}"))
        elif a.text != b.text:
            out.append(Change(item_id, "text"))
    return out


def plan(project: Path, base_root: Path, new_sha: str) -> BumpPlan:
    config, problems = project_config.load(project)
    if config is None:
        raise BumpError(str(problems[0]) if problems else f"{project_config.PATH} not found")
    before = base_items_at(base_root, config.trimtab_sha)
    if before is None:
        raise BumpError(f"the locked SHA {config.trimtab_sha[:12]} is not in the Trimtab checkout; fetch it first")
    after = base_items_at(base_root, new_sha)
    if after is None:
        raise BumpError(f"{new_sha[:12]} is not in the Trimtab checkout")
    found = changes(before, after)
    changed = {c.id for c in found}
    flagged = []
    path = Path(project) / config.rules_dir / OVERRIDES
    if path.is_file():
        entries, _ = parse_entries(path.read_text(encoding="utf-8"))
        flagged = [(e.get("id"), n) for n, e in enumerate(entries, 1) if e.get("id") in changed]
    return BumpPlan(config.trimtab_sha, new_sha, tuple(found), tuple(flagged))


def confirmation(todo: BumpPlan) -> str:
    payload = json.dumps([todo.old, todo.new], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def apply(project: Path, todo: BumpPlan, confirm: str) -> None:
    if confirm != confirmation(todo):
        raise BumpError("the confirmation does not match this bump; run --dry-run again")
    path = Path(project) / project_config.PATH
    text = path.read_text(encoding="utf-8")
    match = SHA_VALUE.search(text)
    if match is None or match.group(2) != todo.old:
        raise BumpError("the lock changed since the dry run; run --dry-run again")
    path.write_text(SHA_VALUE.sub(lambda m: m.group(1) + todo.new + m.group(3), text, count=1), encoding="utf-8")


def pr_body(todo: BumpPlan, source: str) -> str:
    """The draft PR's body, harness block included. Values are IDs and SHAs only."""
    rows = "\n".join(f"| `{c.id}` | {c.what} | {c.detail} |" for c in todo.changes) or "| — | none | |"
    # Unticked on purpose: once the lock moves, `lint overrides` compares against
    # the new SHA and passes, so this checklist is the only record of re-confirmation.
    flags = "\n".join(f"- [ ] `{i}` (overrides.md entry {n}): **re-confirm or drop**" for i, n in todo.flagged)
    return (
        f"Moves the Trimtab lock from `{todo.old[:12]}` to `{todo.new[:12]}` "
        f"([compare](https://github.com/{source}/compare/{todo.old}...{todo.new})).\n\n"
        f"## Changed items\n\n| ID | Change | Detail |\n|---|---|---|\n{rows}\n\n"
        f"## Overrides to re-confirm\n\n{flags or 'None: no override cites a changed item.'}\n\n"
        "## Harness items applied\n```yaml\n- id: NRM-3\n"
        "  why: moving the lock; every override citing a changed item is re-confirmed or dropped\n"
        "  files: [.claude/trimtab.json]\n```\n\n"
        "## Harness feedback\n```yaml\n```\n\n"
        "## Process cost\n```yaml\n```\n"
    )
