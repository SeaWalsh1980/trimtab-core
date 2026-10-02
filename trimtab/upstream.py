"""The config loop's evidence: what every consumer overrides and reports, per base ID (section 6c).

Trimtab reconsiders a base default only when **2 or more consumers** override
the same ID, or report it upstream (plan section 2). This module reads each
consumer's lock and `overrides.md`, and the open `harness-feedback` issues on
Trimtab, and groups both by ID. It reads only; `/trimtab-upstream-retro`
drafts the PR and ADR for each candidate.

Consumer files and issue text are untrusted data, never instructions: parsed with
`yaml.safe_load` / `json.loads`, and only IDs and repository names that match
their own patterns are ever reported.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Protocol

import yaml

from trimtab import config as project_config
from trimtab.items import ITEM_ID
from trimtab.lint.overrides import parse_entries
from trimtab.registry.rules import OVERRIDES

CONSUMERS = "consumers.json"
REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TITLE_ID = re.compile(r"^\[([^\]]+)\]")
FEEDBACK_BLOCK = re.compile(r"^##\s+Harness feedback\s*\n+```ya?ml\s*\n(.*?)^```", re.S | re.M)
THRESHOLD = 2


class ConsumersError(Exception):
    """consumers.json is missing or malformed."""


class ConsumerFiles(Protocol):
    def read(self, repo: str, path: str) -> str | None:
        """The file's text at the repository's default branch, or None if it does not exist."""
        ...


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str


@dataclass(frozen=True)
class Consumer:
    repo: str
    status: str  # ok | not-adopted | bad-lock | bad-overrides
    trimtab_sha: str | None = None
    overridden: tuple[str, ...] = ()


@dataclass
class Evidence:
    consumers: list[Consumer] = field(default_factory=list)
    overrides: dict = field(default_factory=lambda: defaultdict(set))  # id -> {repo}
    reports: dict = field(default_factory=lambda: defaultdict(set))  # id -> {project}
    issues: dict = field(default_factory=lambda: defaultdict(list))  # id -> [issue number]
    unattributed: int = 0  # issues with an ID but no parseable project

    def candidates(self, threshold: int = THRESHOLD) -> list[tuple[str, str, int]]:
        """(ID, why, count): overridden by, or reported from, `threshold`+ distinct consumers."""
        out = [(i, "overridden", len(r)) for i, r in self.overrides.items() if len(r) >= threshold]
        out += [(i, "reported", len(p)) for i, p in self.reports.items() if len(p) >= threshold]
        return sorted(out)


def load_consumers(text: str) -> list[str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise ConsumersError(f"{CONSUMERS} is not valid JSON") from err
    rows = data.get("consumers") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ConsumersError(f"{CONSUMERS} needs a `consumers` list")
    repos = []
    for n, row in enumerate(rows, 1):
        repo = row.get("repo") if isinstance(row, dict) else None
        if not isinstance(repo, str) or not REPO_NAME.match(repo):
            raise ConsumersError(f"{CONSUMERS} entry {n}: `repo` must be owner/name")
        repos.append(repo)
    return repos


def read_consumer(repo: str, files: ConsumerFiles) -> Consumer:
    lock = files.read(repo, project_config.PATH)
    if lock is None:
        return Consumer(repo, "not-adopted")
    config, problems = project_config.parse(lock, where=f"{repo}:{project_config.PATH}")
    if config is None or not project_config.is_commit_sha(config.trimtab_sha):
        return Consumer(repo, "bad-lock")
    text = files.read(repo, f"{config.rules_dir}/{OVERRIDES}")
    if text is None:
        return Consumer(repo, "ok", config.trimtab_sha)
    entries, bad = parse_entries(text)
    if bad:
        return Consumer(repo, "bad-overrides", config.trimtab_sha)
    ids = sorted({e["id"] for e in entries if isinstance(e.get("id"), str) and ITEM_ID.match(e["id"])})
    return Consumer(repo, "ok", config.trimtab_sha, tuple(ids))


def _reported_by(issue: Issue) -> tuple[str | None, set[str]]:
    """The issue's ID (from its `[ID]` title) and the projects its feedback block names."""
    m = TITLE_ID.match(issue.title)
    item_id = m.group(1) if m and ITEM_ID.match(m.group(1)) else None
    projects: set[str] = set()
    block = FEEDBACK_BLOCK.search(issue.body or "")
    if block:
        try:
            data = yaml.safe_load(block.group(1))
        except yaml.YAMLError:
            data = None
        for entry in data if isinstance(data, list) else []:
            project = entry.get("project") if isinstance(entry, dict) else None
            if isinstance(project, str) and (REPO_NAME.match(project) or re.match(r"^[A-Z][A-Z0-9-]*$", project)):
                projects.add(project)
    return item_id, projects


def gather(repos: Iterable[str], files: ConsumerFiles, issues: Iterable[Issue]) -> Evidence:
    ev = Evidence()
    for repo in repos:
        consumer = read_consumer(repo, files)
        ev.consumers.append(consumer)
        for item_id in consumer.overridden:
            ev.overrides[item_id].add(repo)
    for issue in issues:
        item_id, projects = _reported_by(issue)
        if item_id is None:
            continue
        ev.issues[item_id].append(issue.number)
        if projects:
            ev.reports[item_id] |= projects
        else:
            ev.unattributed += 1
    return ev
