"""Moving a project's lock: which items changed, and which overrides must be re-confirmed.

`/trimtab-bump` opens a draft PR moving `trimtab_sha` forward (ADR 0005).
The PR lists every base item that changed between the locked SHA and the new
one, and flags each override citing a changed ID: "re-confirm or drop". The
lock is written only behind a confirmation gate: `plan` reads, `apply` needs the
dry run's token and refuses when the lock has moved since.

The same write records `base_sha`, the base SHA the instance at the new
`trimtab_sha` pins (ADR 0017), so a reader with no access to the instance can
run the base the project was reviewed against. It is derived here and nowhere
else: never chosen by hand.

The lock file is rewritten by replacing values, so its formatting and key
order survive.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from trimtab import config as project_config
from trimtab import instance as instance_file
from trimtab.items import Item
from trimtab.lint.overrides import base_items_at, parse_entries
from trimtab.registry.rules import OVERRIDES

SHA_VALUE = re.compile(r'("trimtab_sha"\s*:\s*")([^"]*)(")')
BASE_VALUE = re.compile(r'("base_sha"\s*:\s*")([^"]*)(")')


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
    base_old: str | None  # the lock's base_sha, if it has one
    base_new: str  # the base the instance at `new` pins
    base_repo: str

    @property
    def is_current(self) -> bool:
        return self.old == self.new and self.base_old == self.base_new


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
    try:
        pinned = instance_file.at_commit(base_root, new_sha)
    except instance_file.InstanceFileError as err:
        raise BumpError(f"cannot read the base the instance pins at {new_sha[:12]}: {err}") from err
    found = changes(before, after)
    changed = {c.id for c in found}
    flagged = []
    path = Path(project) / config.rules_dir / OVERRIDES
    if path.is_file():
        entries, _ = parse_entries(path.read_text(encoding="utf-8"))
        flagged = [(e.get("id"), n) for n, e in enumerate(entries, 1) if e.get("id") in changed]
    return BumpPlan(config.trimtab_sha, new_sha, tuple(found), tuple(flagged),
                    config.base_sha, pinned.base_sha, pinned.base_repo)


def confirmation(todo: BumpPlan) -> str:
    payload = json.dumps([todo.old, todo.new, todo.base_old, todo.base_new], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def _with_base(text: str, base: str) -> str:
    """`text` with base_sha set to `base`: in place if present, else right after trimtab_sha."""
    if BASE_VALUE.search(text):
        return BASE_VALUE.sub(lambda m: m.group(1) + base + m.group(3), text, count=1)
    match = SHA_VALUE.search(text)
    line = text[text.rfind("\n", 0, match.start()) + 1:match.start()]
    separator = ",\n" + line if not line.strip() else ", "  # one key per line, or a one-line lock
    return text[:match.end()] + f'{separator}"base_sha": "{base}"' + text[match.end():]


def apply(project: Path, todo: BumpPlan, confirm: str) -> None:
    if confirm != confirmation(todo):
        raise BumpError("the confirmation does not match this bump; run --dry-run again")
    path = Path(project) / project_config.PATH
    text = path.read_text(encoding="utf-8")
    match, base = SHA_VALUE.search(text), BASE_VALUE.search(text)
    if match is None or match.group(2) != todo.old or (base.group(2) if base else None) != todo.base_old:
        raise BumpError("the lock changed since the dry run; run --dry-run again")
    text = _with_base(SHA_VALUE.sub(lambda m: m.group(1) + todo.new + m.group(3), text, count=1), todo.base_new)
    # The rewrite is textual, so read it back before writing: a nested or duplicate
    # key could take the new value instead of the lock's own, and a consumer's CI
    # runs whatever base_sha says.
    written, _ = project_config.parse(text, where=str(path))
    if written is None or (written.trimtab_sha, written.base_sha) != (todo.new, todo.base_new):
        raise BumpError("the rewritten lock would not read back as the new SHAs (a nested or duplicate "
                        "key?); nothing written, so edit the lock by hand")
    path.write_text(text, encoding="utf-8")


def _base_line(todo: BumpPlan) -> str:
    line = f"`base_sha` becomes `{todo.base_new[:12]}`, the base the new instance pins"
    if todo.base_old is None:
        return line + " (the lock had none)."
    if todo.base_old == todo.base_new:
        return line + " (unchanged)."
    return (line + f" (was `{todo.base_old[:12]}`; "
            f"[compare](https://github.com/{todo.base_repo}/compare/{todo.base_old}...{todo.base_new})).")


def pr_body(todo: BumpPlan, repo: str) -> str:
    """The draft PR's body, harness block included. Values are IDs and SHAs only.

    The applied block cites `id: none`: the base defines no item IDs of its
    own, so any ID written here would belong to one instance and be unknown to
    every other. The why is quoted so a later edit adding `: ` still parses.
    """
    rows = "\n".join(f"| `{c.id}` | {c.what} | {c.detail} |" for c in todo.changes) or "| — | none | |"
    # Unticked on purpose: once the lock moves, `lint overrides` compares against
    # the new SHA and passes, so this checklist is the only record of re-confirmation.
    flags = "\n".join(f"- [ ] `{i}` (overrides.md entry {n}): **re-confirm or drop**" for i, n in todo.flagged)
    return (
        f"Moves the Trimtab lock from `{todo.old[:12]}` to `{todo.new[:12]}` "
        f"([compare](https://github.com/{repo}/compare/{todo.old}...{todo.new})).\n\n"
        f"{_base_line(todo)}\n\n"
        f"## Changed items\n\n| ID | Change | Detail |\n|---|---|---|\n{rows}\n\n"
        f"## Overrides to re-confirm\n\n{flags or 'None: no override cites a changed item.'}\n\n"
        "## Harness items applied\n```yaml\n- id: none\n"
        '  why: "moving the lock; every override citing a changed item is re-confirmed or dropped"\n'
        "  files: [.claude/trimtab.json]\n```\n\n"
        "## Harness feedback\n```yaml\n```\n\n"
        "## Process cost\n```yaml\n```\n"
    )
