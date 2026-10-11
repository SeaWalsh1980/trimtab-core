"""The IDs a project may cite: the base registry plus the project's own items."""

from __future__ import annotations

from pathlib import Path

from trimtab import config as project_config
from trimtab.items import Item, Problem
from trimtab.registry import harness
from trimtab.registry.rules import RuleType


def registry_for(base_root: Path, project: Path | None = None) -> tuple[dict[str, Item], list[Problem]]:
    found = harness.discover(Path(base_root))
    items = {i.id: i for i in found.items}
    problems = list(found.problems)
    if project is not None and Path(project).resolve() != Path(base_root).resolve():
        local, _, bad = project_items(Path(project))
        items.update(local)
        problems += bad
    return items, problems


def project_items(project: Path) -> tuple[dict[str, Item], str | None, list[Problem]]:
    """The project's own items and its `id_prefix` (None with no lock), reading no instance.

    A lock or rule file that cannot be read is a problem, never an empty result.
    """
    config, problems = project_config.load(Path(project))
    if config is None:
        return {}, None, problems
    local = RuleType(rules_dir=config.rules_dir, project_prefix=config.id_prefix).discover(Path(project))
    return {i.id: i for i in local.items}, config.id_prefix, problems + list(local.problems)
