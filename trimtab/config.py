"""`.claude/trimtab.json`: a project's lock and settings (plan section 3)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from trimtab.items import Problem

PATH = ".claude/trimtab.json"
SCHEMA_VERSION = 1
REQUIRED = ("source", "trimtab_sha", "id_prefix", "schema_version")
COMMIT_SHA = re.compile(r"^[0-9a-f]{7,40}$")


def is_commit_sha(value) -> bool:
    """A hex object name, and nothing git could read as an option or a range.

    A lock's `trimtab_sha` reaches git as an argument, and a consumer's lock is
    untrusted data: anything else is refused before git sees it.
    """
    return isinstance(value, str) and COMMIT_SHA.match(value) is not None


@dataclass(frozen=True)
class ProjectConfig:
    source: str
    trimtab_sha: str
    id_prefix: str
    schema_version: int = SCHEMA_VERSION
    item_types: tuple[str, ...] = ("rule",)
    reviewer_agent: str = "trimtab-reviewer"
    planner_agent: str = "trimtab-planner"
    rules_dir: str = ".claude/rules"
    always_loaded_budget_kb: int = 50
    cadence: str = "weekly"
    adopted_at: str | None = None
    baseline_pr: int = 0
    routines: dict = field(default_factory=dict, compare=False)


def parse(text: str, where: str = PATH) -> tuple[ProjectConfig | None, list[Problem]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, [Problem("bad-config", where, "not valid JSON")]
    if not isinstance(data, dict):
        return None, [Problem("bad-config", where, "expected a JSON object")]
    missing = [k for k in REQUIRED if k not in data]
    if missing:
        return None, [Problem("bad-config", where, f"missing {', '.join(missing)}")]
    known = {f for f in ProjectConfig.__dataclass_fields__}
    values = {k: v for k, v in data.items() if k in known}
    if "item_types" in values:
        values["item_types"] = tuple(values["item_types"])
    try:
        config = ProjectConfig(**values)
    except TypeError:
        return None, [Problem("bad-config", where, "unexpected field types")]
    if not isinstance(config.baseline_pr, int) or not isinstance(config.schema_version, int):
        return None, [Problem("bad-config", where, "baseline_pr and schema_version must be integers")]
    return config, []


def load(project: Path) -> tuple[ProjectConfig | None, list[Problem]]:
    """(None, []) when the project has not adopted Trimtab."""
    path = Path(project) / PATH
    if not path.is_file():
        return None, []
    return parse(path.read_text(encoding="utf-8"), where=str(path))
