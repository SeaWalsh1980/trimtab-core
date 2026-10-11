"""`trimtab lint overrides`: a project's deviations from base rules, declared in one place.

`.claude/rules/overrides.md` holds one ```yaml list. Each entry names a base
ID (and optionally the clause's first words), the replacement `text`, the
`reason` and an `adr` path in the project. Every check is structural, so the
result is deterministic (ADR 0005):

- the ID exists in the base and is not withdrawn;
- the item or clause is PREFER or AVOID (REQUIRE/PROHIBIT go upstream);
- reason, text and a resolvable ADR link are present;
- the base text of the ID is unchanged since the project's `trimtab_sha`.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

from trimtab import config as project_config
from trimtab.items import Item, Problem, Strength
from trimtab.registry.rules import OVERRIDES, RuleType, clause_line, clause_strength, parse_rule_file

OVERRIDABLE = (Strength.PREFER, Strength.AVOID)
YAML_BLOCK = re.compile(r"^```ya?ml\s*\n(.*?)^```\s*$", re.S | re.M)
GIT_TIMEOUT_SECONDS = 30


def _git(root: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def base_items_at(base_root: Path, sha: str, rules_dir: str = "rules") -> dict[str, Item] | None:
    """The base registry as it stood at `sha`, or None if git cannot show it."""
    if not project_config.is_commit_sha(sha):
        return None  # never hand git an option or a range as a revision
    names = _git(base_root, "ls-tree", "--name-only", sha, "--", f"{rules_dir}/")
    if names is None:
        return None
    items: dict[str, Item] = {}
    for name in names.split():
        if not name.endswith(".md"):
            continue
        text = _git(base_root, "show", f"{sha}:{name}")
        if text is None:
            return None
        found, _ = parse_rule_file(name, text)
        items.update({i.id: i for i in found})
    return items


def parse_entries(text: str) -> tuple[list, list[Problem]]:
    match = YAML_BLOCK.search(text)
    if match is None:
        return [], [Problem("not-yaml", OVERRIDES, "expected one ```yaml list of entries")]
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return [], [Problem("not-yaml", OVERRIDES, "the yaml block does not parse")]
    if data is None:
        return [], []
    if not isinstance(data, list) or not all(isinstance(e, dict) for e in data):
        return [], [Problem("bad-shape", OVERRIDES, "expected a list of mappings")]
    return data, []


def _clause_text(item: Item, clause: str | None) -> str:
    return item.text if not clause else (clause_line(item, clause) or "")


def lint(project: Path, base_root: Path) -> list[Problem]:
    project, base_root = Path(project), Path(base_root)
    config, problems = project_config.load(project)
    if config is None:
        return problems
    path = project / config.rules_dir / OVERRIDES
    if not path.is_file():
        return []
    entries, problems = parse_entries(path.read_text(encoding="utf-8"))
    now = {i.id: i for i in RuleType().discover(base_root).items}
    locked = base_items_at(base_root, config.trimtab_sha)
    if entries and locked is None:
        problems.append(Problem("unverifiable", project_config.PATH,
                                "trimtab_sha is not in the base checkout; cannot tell whether base text changed"))
    for n, entry in enumerate(entries, 1):
        where = f"{OVERRIDES}, entry {n}"
        item_id, clause = entry.get("id"), entry.get("clause")
        for key in ("reason", "text", "adr"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                problems.append(Problem("missing-field", where, f"{key} is required"))
        adr = entry.get("adr")
        if isinstance(adr, str) and adr.strip() and not (project / adr).is_file():
            problems.append(Problem("missing-adr", where, "the adr path does not exist in the project"))
        item = now.get(item_id) if isinstance(item_id, str) else None
        if item is None:
            problems.append(Problem("unknown-id", where, "id is not a base item"))
            continue
        if item.withdrawn:
            problems.append(Problem("withdrawn-id", where, "the base item is withdrawn"))
        strength = clause_strength(item, clause) if clause else item.strength
        if strength is None:
            problems.append(Problem("unknown-clause", where, "no line of the item starts with the clause"))
            continue
        if strength.binding:
            problems.append(Problem("binding-override", where,
                                    f"{item_id} is {strength.name}: raise an upstream issue instead"))
        elif strength not in OVERRIDABLE:
            problems.append(Problem("not-overridable", where, "only PREFER or AVOID may be overridden"))
        if locked is not None:
            before = locked.get(item_id)
            if before is None or _clause_text(before, clause) != _clause_text(item, clause):
                problems.append(Problem("changed-text", where,
                                        "base text changed since trimtab_sha: re-confirm or drop the override"))
    return problems
