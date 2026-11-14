"""`.claude/trimtab.json`: a project's lock and settings (ADR 0005)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from trimtab.items import Problem

PATH = ".claude/trimtab.json"
SCHEMA_VERSION = 2
REQUIRED = ("trimtab_sha", "id_prefix", "schema_version")
# Schema 1 bindings. The instance's instance.json names its repository, and no
# file holds a routine ID.
RETIRED = ("source", "routines")
COMMIT_SHA = re.compile(r"^[0-9a-f]{7,40}$")
# `base_sha` is read by consumers' CI to choose which base code runs there, so
# only a full SHA: a short one can become ambiguous, and a ref can move.
FULL_SHA = re.compile(r"[0-9a-f]{40}")  # used with `fullmatch`: `$` would also accept a trailing newline


def is_commit_sha(value) -> bool:
    """A hex object name, and nothing git could read as an option or a range.

    A lock's `trimtab_sha` reaches git as an argument, and a consumer's lock is
    untrusted data: anything else is refused before git sees it.
    """
    return isinstance(value, str) and COMMIT_SHA.match(value) is not None


@dataclass(frozen=True)
class ProjectConfig:
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
    # Derived, never chosen: the base SHA the instance at `trimtab_sha` pins,
    # written by `trimtab bump` for a reader with no access to the instance.
    base_sha: str | None = None
    # Derived, never chosen: E1-E23 as the doctrine record at `trimtab_sha` states them, the project's jobs for
    # them, and the base's repository, written by `trimtab bump` so `check-enforcement --server` needs no
    # instance checkout (ADR 0063 §2, §7). Shape: `check_enforcement`.
    enforcement: dict | None = None
    warnings: tuple[str, ...] = field(default=(), compare=False)


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
    version = data.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        return None, [Problem("bad-config", where, "schema_version must be an integer")]
    if version > SCHEMA_VERSION:
        return None, [Problem("bad-config", where, f"schema_version {version} is newer than this base "
                                                   f"({SCHEMA_VERSION}); update the base")]
    retired = [k for k in RETIRED if k in data]
    warnings: tuple[str, ...] = ()
    if retired and version >= 2:
        return None, [Problem("bad-config", where, f"{', '.join(retired)} is not part of schema "
                                                   f"{version}; remove it (the instance's instance.json "
                                                   "names the repository)")]
    if retired:
        warnings = (f"schema_version {version}: {', '.join(retired)} ignored; "
                    "the instance's instance.json names the repository",)
    known = {f for f in ProjectConfig.__dataclass_fields__}
    values = {k: v for k, v in data.items() if k in known and k != "warnings"}
    if "item_types" in values:
        values["item_types"] = tuple(values["item_types"])
    try:
        config = ProjectConfig(**values, warnings=warnings)
    except TypeError:
        return None, [Problem("bad-config", where, "unexpected field types")]
    if not isinstance(config.baseline_pr, int) or not isinstance(config.schema_version, int):
        return None, [Problem("bad-config", where, "baseline_pr and schema_version must be integers")]
    if config.base_sha is not None and not (isinstance(config.base_sha, str) and FULL_SHA.fullmatch(config.base_sha)):
        return None, [Problem("bad-config", where, "base_sha must be a full 40-character lower-case commit SHA; "
                                                   "remove it and re-run /trimtab-bump, which derives it")]
    if config.enforcement is not None:
        why = check_enforcement(config.enforcement)
        if why is None and config.base_sha is None:
            why = "it needs base_sha, which names the base its rows are judged against"
        if why is not None:
            return None, [Problem("bad-config", where, f"enforcement: {why}; remove it and re-run "
                                                       "/trimtab-bump, which derives it")]
    return config, []


ROW_IDS = tuple(f"E{n}" for n in range(1, 24))
ROW_KEYS = {"owner", "sha256"}
JOB_KEYS = {"workflow", "job"}
FIELD_KEYS = {"base_repo", "rows", "jobs", "instance"}
# The instance keys the plan-run rows read, which the server job has no instance to read them from.
PLAN_KEYS = ("accounts", "secret_scan", "review", "fix_limits")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
WORKFLOW = re.compile(r"^\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml$")
JOB_NAME = re.compile(r"^[^\x00-\x1f]{1,100}$")


def check_enforcement(value) -> str | None:
    """Why `value` is not a derived enforcement field, or None. Pure; the owners are the record reader's."""
    from trimtab.enforcement.record import OWNERS  # the one place the owner names are written

    if not isinstance(value, dict) or set(value) != FIELD_KEYS:
        return f"must be an object with exactly {', '.join(sorted(FIELD_KEYS))}"
    if (not isinstance(value["base_repo"], str) or not REPO.fullmatch(value["base_repo"])
            or any(part in (".", "..") for part in value["base_repo"].split("/"))):  # as the instance reader
        return "base_repo must be owner/name"
    rows, jobs = value["rows"], value["jobs"]
    if not isinstance(rows, dict) or tuple(rows) != ROW_IDS:
        missing = [r for r in ROW_IDS if not isinstance(rows, dict) or r not in rows]
        return f"rows must be exactly E1-E23, in order (missing: {', '.join(missing) or 'none'})"
    for rid, row in rows.items():
        if not isinstance(row, dict) or set(row) != ROW_KEYS:
            return f"{rid} must be an object with owner and sha256"
        if not isinstance(row["owner"], str) or row["owner"] not in OWNERS:
            return f"{rid}: owner is not one the base knows"
        if not isinstance(row["sha256"], str) or not DIGEST.fullmatch(row["sha256"]):
            return f"{rid}: sha256 must be a 64-character lower-case digest"
    if not isinstance(jobs, dict):
        return "jobs must be an object"
    for rid, ref in jobs.items():
        if rid not in rows:
            return f"jobs names {rid!r}, which is not a row"
        if not isinstance(ref, dict) or set(ref) != JOB_KEYS:
            return f"{rid}: a job needs workflow and job"
        if not isinstance(ref["workflow"], str) or not WORKFLOW.fullmatch(ref["workflow"]):
            return f"{rid}: workflow must be .github/workflows/<file>.yml"
        if not isinstance(ref["job"], str) or not JOB_NAME.fullmatch(ref["job"]):
            return f"{rid}: job must be a name of 1-100 printable characters"
    return _check_instance_keys(value["instance"])


def _check_instance_keys(keys) -> str | None:
    from trimtab import instance  # the instance file's own reader decides what each key may hold

    if not isinstance(keys, dict) or not set(keys) <= set(PLAN_KEYS):
        return f"instance must be an object holding only {', '.join(PLAN_KEYS)}"
    placeholder = "owner/name"  # the keys are judged alone; the file's own repo and base are not in question
    try:
        instance.parse({"schema_version": instance.SCHEMA_VERSION, "repo": placeholder,
                        "base": {"repo": placeholder, "sha": "0" * 40}, **keys})
    except instance.InstanceFileError as err:
        return f"instance: {err}"
    except (TypeError, ValueError, AttributeError, KeyError) as err:  # an odd value type is a bad lock, not a crash
        return f"instance: unexpected value ({type(err).__name__})"
    return None


def load(project: Path) -> tuple[ProjectConfig | None, list[Problem]]:
    """(None, []) when the project has not adopted Trimtab."""
    path = Path(project) / PATH
    if not path.is_file():
        return None, []
    return parse(path.read_text(encoding="utf-8"), where=str(path))
