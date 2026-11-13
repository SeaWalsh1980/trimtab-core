"""Actions job records, read the one way every gate reads them (ADR 0058, ADR 0063 §2).

A session's token cannot read check runs, so gates read Actions' job records instead: every
workflow run on a head, and every job of each run on every attempt (`filter=all`). A list is
read in full or refused, never cut short, and a job counts as a pass only with a runner and
steps. `Reader` memoises the lists for one unit of work (ENG-6.2), so every row judged in it is
judged from one snapshot.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from trimtab.capture.ghapi import PER_PAGE, GhApi, SourceError

LIMIT = 200  # runs on one head, or jobs in one run, every attempt; past it, an error


class ActionsError(Exception):
    """A job-record read failed or returned the wrong shape."""


def rows(read_json: Callable[[str, str, dict], object], path: str, key: str, what: str, limit: int, query: dict,
         error: type[Exception] = ActionsError) -> list:
    """Every row of an Actions list, every page, wrapped in an object under `key`; past `limit`, `error`."""
    out: list = []
    page = 1
    while True:
        data = read_json(path, what, {**query, "per_page": PER_PAGE, "page": page})
        batch = data.get(key) if isinstance(data, dict) else None
        if not isinstance(batch, list):
            raise error(f"{what} returned unexpected JSON")
        out += batch
        if len(out) > limit:
            raise error(f"{what} holds more than {limit} rows; refusing a partial list")
        if len(batch) < PER_PAGE:
            return out
        page += 1


@dataclass(frozen=True)
class Run:
    id: int
    head_sha: str
    head_branch: str | None
    path: str
    event: str
    status: str


@dataclass(frozen=True)
class Job:
    name: str
    head_sha: str
    status: str
    conclusion: str
    runner_id: int | None
    runner_name: str
    steps: int

    @property
    def passed(self) -> bool:
        return (self.status == "completed" and self.conclusion == "success" and self.runner_id is not None
                and bool(self.runner_name) and self.steps > 0)


def _id(value) -> bool:
    return type(value) is int and value > 0


def _run(row) -> Run:
    if not isinstance(row, dict) or not _id(row.get("id")):
        raise ActionsError("gh api actions runs returned unexpected JSON")
    head, path, event, status = (row.get(k) for k in ("head_sha", "path", "event", "status"))
    branch = row.get("head_branch")
    if not all(isinstance(v, str) for v in (head, path, event, status)) \
            or not (branch is None or isinstance(branch, str)):
        raise ActionsError("gh api actions runs returned unexpected JSON")
    return Run(row["id"], head, branch, path, event, status)


def _job(row) -> Job:
    if not isinstance(row, dict):
        raise ActionsError("gh api actions jobs returned unexpected JSON")
    steps, runner_id = row.get("steps"), row.get("runner_id")
    name, head, status = row.get("name"), row.get("head_sha"), row.get("status")
    conclusion, runner_name = row.get("conclusion") or "", row.get("runner_name") or ""
    if not all(isinstance(v, str) for v in (name, head, status, conclusion, runner_name)) \
            or not isinstance(steps, list) or not (runner_id is None or _id(runner_id)):
        raise ActionsError("gh api actions jobs returned unexpected JSON")
    return Job(name, head, status, conclusion, runner_id, runner_name, len(steps))


class Reader:
    """Memoised job-record reads for one unit of work."""

    def __init__(self, api: GhApi):
        self._api = api
        self._runs: dict[tuple[str, str, str], tuple[Run, ...]] = {}
        self._jobs: dict[tuple[str, int], tuple[Job, ...]] = {}

    def _json(self, path: str, what: str, query: dict):
        try:
            return self._api.json("GET", path, what, query=query)
        except SourceError as err:
            raise ActionsError(str(err)) from err

    def runs(self, repository: str, head: str, event: str) -> tuple[Run, ...]:
        key = (repository.casefold(), head, event)
        if key not in self._runs:
            found = rows(self._json, f"repos/{repository}/actions/runs", "workflow_runs", "gh api actions runs",
                         LIMIT, {"head_sha": head, "event": event})
            self._runs[key] = tuple(_run(r) for r in found)
        return self._runs[key]

    def jobs(self, repository: str, run_id: int) -> tuple[Job, ...]:
        key = (repository.casefold(), run_id)
        if key not in self._jobs:
            found = rows(self._json, f"repos/{repository}/actions/runs/{run_id}/jobs", "jobs",
                         "gh api actions jobs", LIMIT, {"filter": "all"})
            self._jobs[key] = tuple(_job(r) for r in found)
        return self._jobs[key]

    def job_problem(self, repository: str, head: str, workflow: str, job: str, event: str = "push",
                    branch: str | None = None) -> str | None:
        """Why `job` of `workflow` on `head` for `event` is not a pass on every attempt of every run, or None.

        With `branch`, every such run must be for that branch: a run of the same commit pushed to
        another branch does not stand for it."""
        mine = [r for r in self.runs(repository, head, event)
                if r.path == workflow and r.head_sha == head and r.event == event]
        if not mine:
            return f"no {event} run of {workflow} on {head[:12]}"
        if branch is not None and any(r.head_branch != branch for r in mine):
            return f"a run of {workflow} on {head[:12]} is for another branch than {branch}"
        if any(r.status != "completed" for r in mine):
            return f"a run of {workflow} on {head[:12]} has not finished"
        for run in mine:
            named = [j for j in self.jobs(repository, run.id) if j.name == job]
            if not named:
                return f"a run of {workflow} on {head[:12]} lists no {job} job"
            if not all(j.passed and j.head_sha == head for j in named):
                return f"a {job} attempt on {head[:12]} is not a completed, passing job with a runner and steps"
        return None
