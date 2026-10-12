"""Infrastructure: where merged PRs come from. GitHub through `gh api`, or a fixture file.

Every GitHub call is `gh api` on a REST path. gh's porcelain (`gh pr list`,
`gh issue create`, `gh repo view`, ...) goes through GraphQL, which Claude Code
cloud sessions refuse, so a routine that used it could not run at all. `GhApi`
refuses a GraphQL path outright, and every `Gh*` class goes through it.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import quote, urlencode

from trimtab.capture.ingest import PullRequest
from trimtab.dependabot import CiRun, HostError, PullRequestView
from trimtab.upstream import Issue

GH_TIMEOUT_SECONDS = 60
# REST's largest page; a list call reads pages of this size until its limit.
PER_PAGE = 100
# GitHub's search API returns at most this many results for one query.
SEARCH_RESULT_CAP = 1000


class SourceError(HostError):
    """gh failed, timed out, or returned something unexpected."""


class NotFound(SourceError):
    """gh api answered 404."""


# Runs one `gh` command line with optional stdin; the default is a subprocess.
# Tests pass an in-memory stand-in: gh is a true external system.
Run = Callable[[list, str | None], subprocess.CompletedProcess]


def _subprocess_run(argv: list, stdin: str | None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS)


class GhApi:
    """`gh api` on REST paths, never GraphQL."""

    def __init__(self, run: Run | None = None):
        self._run = run or _subprocess_run

    def call(self, method: str, path: str, query: Mapping | None = None, body=None,
             accept: str | None = None) -> str:
        if path.strip("/").split("/")[0].split("?")[0] == "graphql":
            raise SourceError("gh api graphql is not used: Claude Code cloud sessions refuse GitHub GraphQL")
        argv = ["gh", "api", "-X", method, path + (f"?{urlencode(query)}" if query else "")]
        if accept:
            argv += ["-H", f"Accept: {accept}"]
        stdin = None
        if body is not None:
            # The body goes in on stdin, never in the arguments.
            argv += ["--input", "-"]
            stdin = json.dumps(body)
        try:
            done = self._run(argv, stdin)
        except (OSError, subprocess.TimeoutExpired) as err:
            raise SourceError(f"gh api {method} {path}: {type(err).__name__}") from err
        if done.returncode != 0:
            message = f"gh api {method} {path} exited {done.returncode}: {done.stderr.strip()[:300]}"
            if "HTTP 404" in done.stderr or "Not Found" in done.stderr:
                raise NotFound(message)
            raise SourceError(message)
        return done.stdout

    def json(self, method: str, path: str, what: str, **kwargs):
        out = self.call(method, path, **kwargs)
        try:
            return json.loads(out)
        except ValueError as err:
            raise SourceError(f"{what} returned unexpected JSON") from err

    def pages(self, path: str, query: Mapping, limit: int, what: str, key: str | None = None) -> list:
        """Up to `limit` rows of a list endpoint, page by page; a short page is the last one."""
        per_page = min(PER_PAGE, limit)
        rows: list = []
        page = 1
        while len(rows) < limit:
            data = self.json("GET", path, what, query={**query, "per_page": per_page, "page": page})
            if isinstance(data, dict) and data.get("incomplete_results") is True:
                # Search timed out on part of the index: a partial answer would move the watermark wrongly.
                raise SourceError(f"{what} returned incomplete results; run again")
            batch = data.get(key) if key is not None and isinstance(data, dict) else data
            if not isinstance(batch, list):
                raise SourceError(f"{what} returned unexpected JSON")
            rows += batch
            if len(batch) < per_page:
                break
            page += 1
        return rows[:limit]


def _search_pulls(api: GhApi, query: str, order: str, limit: int) -> list[PullRequest]:
    if limit > SEARCH_RESULT_CAP:
        raise SourceError(f"GitHub search returns at most {SEARCH_RESULT_CAP} results; lower the limit")
    what = "gh api search/issues"
    rows = api.pages("search/issues", {"q": query, "sort": "created", "order": order}, limit, what, key="items")
    try:
        return [PullRequest(int(r["number"]), r.get("body") or "", r["pull_request"].get("merged_at") or "")
                for r in rows]
    except (KeyError, TypeError, AttributeError, ValueError) as err:
        raise SourceError(f"{what} returned unexpected JSON") from err


class GhSource:
    def __init__(self, repo: str, run: Run | None = None):
        self.repo = repo
        self.api = GhApi(run)

    def unlabelled_merged(self, label: str, merged_since: str | None, limit: int) -> list[PullRequest]:
        # Exact watermark query (ADR 0005), oldest first so a bounded run
        # makes progress rather than re-reading the newest PRs.
        query = f"repo:{self.repo} is:pr is:merged -label:{label}"
        if merged_since:
            query += f" merged:>={merged_since}"
        return _search_pulls(self.api, query, "asc", limit)

    def labelled_merged(self, label: str, merged_since: str, limit: int) -> list[PullRequest]:
        # Newest first: the window is recent history, and a run that hits the
        # limit keeps the most recent evidence (the caller reports truncation).
        query = f"repo:{self.repo} is:pr is:merged label:{label} merged:>={merged_since}"
        return _search_pulls(self.api, query, "desc", limit)

    def add_label(self, number: int, label: str) -> None:
        # A PR's labels are its issue's labels.
        self.api.call("POST", f"repos/{self.repo}/issues/{number}/labels", body={"labels": [label]})

    def body(self, number: int) -> str:
        data = self.api.json("GET", f"repos/{self.repo}/pulls/{number}", "gh api pulls")
        if not isinstance(data, dict):
            raise SourceError("gh api pulls returned unexpected JSON")
        return data.get("body") or ""


class FixtureSource:
    """PRs from a JSON file: `[{"number", "body", "merged_at", "labels"}]`.

    Labels are written back to the file, so a scratch run behaves like a repo.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def _rows(self) -> list[dict]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def unlabelled_merged(self, label: str, merged_since: str | None, limit: int) -> list[PullRequest]:
        rows = sorted(self._rows(), key=lambda r: r["number"])
        todo = [r for r in rows if label not in r.get("labels", [])
                and (merged_since is None or r["merged_at"][:10] >= merged_since)]
        return [PullRequest(r["number"], r.get("body", ""), r["merged_at"]) for r in todo[:limit]]

    def labelled_merged(self, label: str, merged_since: str, limit: int) -> list[PullRequest]:
        rows = sorted(self._rows(), key=lambda r: r["number"], reverse=True)
        done = [r for r in rows if label in r.get("labels", []) and r["merged_at"][:10] >= merged_since]
        return [PullRequest(r["number"], r.get("body", ""), r["merged_at"]) for r in done[:limit]]

    def add_label(self, number: int, label: str) -> None:
        rows = self._rows()
        for r in rows:
            if r["number"] == number and label not in r.setdefault("labels", []):
                r["labels"].append(label)
        self.path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


GIT_TIMEOUT_SECONDS = 30


def changed_files(root: Path, base: str) -> list[str]:
    """Files changed on HEAD since its merge base with `base`, as `git diff base...HEAD` lists them."""
    try:
        done = subprocess.run(["git", "-C", str(root), "diff", "--name-only", "--no-renames", f"{base}...HEAD"],
                              capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise SourceError(f"git diff: {type(err).__name__}") from err
    if done.returncode != 0:
        raise SourceError(f"git diff {base}...HEAD exited {done.returncode}: {done.stderr.strip()[:300]}")
    return [line for line in done.stdout.splitlines() if line.strip()]


ISSUE_LIMIT = 500  # bounded: issues one label listing reads


def _issues_only(rows: list, what: str) -> list[dict]:
    """The issues endpoint lists pull requests too; they carry a `pull_request` key."""
    if not all(isinstance(r, dict) for r in rows):
        raise SourceError(f"{what} returned unexpected JSON")
    return [r for r in rows if "pull_request" not in r]


class GhIssues:
    """Issues on one repository (Trimtab, for upstream proposals). The body goes in on stdin."""

    def __init__(self, repo: str, run: Run | None = None):
        self.repo = repo
        self.destination = repo
        self.api = GhApi(run)

    def recent_titles(self, label: str, closed_since: str | None) -> list[str]:
        what = "gh api issues"
        rows = self._issues(label, {"state": "open"})
        if closed_since:
            # `since` bounds by update time, and an issue is updated when it closes,
            # so it narrows the listing without losing one closed in the window.
            closed = self._issues(label, {"state": "closed", "since": f"{closed_since}T00:00:00Z"})
            try:
                rows += [r for r in closed if (r.get("closed_at") or "")[:10] >= closed_since]
            except TypeError as err:
                raise SourceError(f"{what} returned unexpected JSON") from err
        try:
            return [r["title"] for r in rows]
        except KeyError as err:
            raise SourceError(f"{what} returned unexpected JSON") from err

    def _issues(self, label: str, filters: Mapping[str, str]) -> list[dict]:
        what = "gh api issues"
        rows = self.api.pages(f"repos/{self.repo}/issues", {"labels": label, **filters}, ISSUE_LIMIT, what)
        return _issues_only(rows, what)

    def create(self, title: str, body: str, labels) -> str:
        data = self.api.json("POST", f"repos/{self.repo}/issues", "gh api issues",
                             body={"title": title, "body": body, "labels": list(labels)})
        if not (isinstance(data, dict) and isinstance(data.get("html_url"), str)):
            raise SourceError("gh api issues returned unexpected JSON")
        return data["html_url"]


SHA = re.compile(r"[0-9a-f]{40}")


def _is_int(value) -> bool:
    """An integer that is not a bool: Python counts True and False as integers."""
    return isinstance(value, int) and not isinstance(value, bool)


def _pr_view(r) -> PullRequestView:
    what = "gh api pulls"
    try:
        user = r["user"]
        user_type = user["type"]
        merged_at = r.get("merged_at")
        state = r["state"]
        view = PullRequestView(number=r["number"], author_login=user["login"],
                               author_is_bot=user_type == "Bot" if isinstance(user_type, str) else None,
                               body=r.get("body") or "", head_sha=r["head"]["sha"],
                               state=("MERGED" if merged_at else state.upper()) if isinstance(state, str) else state)
    except (KeyError, TypeError) as err:
        raise SourceError(f"{what} returned unexpected JSON") from err
    if not (_is_int(view.number) and isinstance(view.author_login, str)
            and isinstance(view.author_is_bot, bool) and isinstance(view.body, str)
            and isinstance(view.state, str)):
        raise SourceError(f"{what} returned unexpected JSON")
    if not (isinstance(view.head_sha, str) and SHA.fullmatch(view.head_sha)):
        raise SourceError(f"{what} returned a head SHA that is not 40 hex characters")
    return view


def _runs(data) -> list[CiRun]:
    what = "gh api actions runs"
    try:
        runs = [CiRun(id=r["id"], status=r["status"], conclusion=r.get("conclusion") or "")
                for r in data["workflow_runs"]]
    except (KeyError, TypeError, AttributeError) as err:
        raise SourceError(f"{what} returned unexpected JSON") from err
    if not all(_is_int(r.id) and isinstance(r.status, str) and isinstance(r.conclusion, str) for r in runs):
        raise SourceError(f"{what} returned unexpected JSON")
    return runs


class GhPullRequests:
    """One repository's PRs and their CI runs, for dependabot-block. The body goes in on stdin."""

    def __init__(self, repo: str, run: Run | None = None):
        self.repo = repo
        self.api = GhApi(run)

    def view(self, number: int) -> PullRequestView:
        return _pr_view(self.api.json("GET", f"repos/{self.repo}/pulls/{number}", "gh api pulls"))

    def set_body(self, number: int, body: str) -> None:
        # REST, as every call here is; `gh pr edit` also queries classic
        # project cards and fails where GitHub has sunset them.
        self.api.call("PATCH", f"repos/{self.repo}/pulls/{number}", body={"body": body})

    def runs(self, head_sha: str, workflow: str, limit: int) -> list[CiRun]:
        # REST names a workflow by its file name (or ID), not its display name.
        data = self.api.json("GET", f"repos/{self.repo}/actions/workflows/{quote(workflow, safe='')}/runs",
                             "gh api actions runs",
                             query={"head_sha": head_sha, "event": "pull_request", "per_page": limit})
        return _runs(data)

    def rerun_failed(self, run_id: int) -> None:
        self.api.call("POST", f"repos/{self.repo}/actions/runs/{run_id}/rerun-failed-jobs")


class FixtureIssues:
    """Issues in a JSON file: `[{"title", "body", "labels", "state", "closed_at"}]`; created ones are appended."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.destination = f"fixture:{self.path}"

    def _rows(self) -> list[dict]:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []

    def recent_titles(self, label: str, closed_since: str | None) -> list[str]:
        def recent(r):
            if r.get("state", "open") == "open":
                return True
            return closed_since is not None and (r.get("closed_at") or "")[:10] >= closed_since
        return [r["title"] for r in self._rows() if label in r.get("labels", []) and recent(r)]

    def create(self, title: str, body: str, labels) -> str:
        rows = self._rows()
        rows.append({"title": title, "body": body, "labels": list(labels), "state": "open"})
        self.path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        return f"fixture issue {len(rows)}"


class GhContents:
    """A consumer's files at its default branch, through the contents API."""

    def __init__(self, run: Run | None = None):
        self.api = GhApi(run)

    def read(self, repo: str, path: str) -> str | None:
        try:
            return self.api.call("GET", f"repos/{repo}/contents/{path}", accept="application/vnd.github.raw")
        except NotFound:
            return None


class FixtureContents:
    """Consumer files under a directory: `<dir>/<owner>/<name>/<path>`."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def read(self, repo: str, path: str) -> str | None:
        f = self.root / repo / path
        return f.read_text(encoding="utf-8") if f.is_file() else None


def open_feedback_issues(repo: str, label: str, limit: int = ISSUE_LIMIT, run: Run | None = None) -> list[Issue]:
    what = "gh api issues"
    rows = _issues_only(GhApi(run).pages(f"repos/{repo}/issues", {"labels": label, "state": "open"}, limit, what),
                        what)
    try:
        return [Issue(int(r["number"]), r.get("title") or "", r.get("body") or "") for r in rows]
    except (ValueError, KeyError, TypeError) as err:
        raise SourceError(f"{what} returned unexpected JSON") from err
