"""The IDs a project may cite: the base registry plus the project's own items."""

from __future__ import annotations

from pathlib import Path

from trimtab import config as project_config
from trimtab.items import Item, Problem
from trimtab.registry import harness
from trimtab.registry.rules import RuleType


def registry_for(base_root: Path, project: Path | None = None) -> tuple[dict[str, Item], list[Problem]]:
    found = harness.discover(Path(base_root))
    items = list(found.items)
    problems = list(found.problems)
    if project is not None and Path(project).resolve() != Path(base_root).resolve():
        config, bad = project_config.load(Path(project))
        problems += bad
        if config is not None:
            local = RuleType(rules_dir=config.rules_dir, project_prefix=config.id_prefix).discover(Path(project))
            items += local.items
            problems += local.problems
    return {i.id: i for i in items}, problems
