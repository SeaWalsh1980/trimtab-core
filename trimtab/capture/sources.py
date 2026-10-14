"""Infrastructure: where merged PRs come from. GitHub through `gh api`, or a fixture file.

Every GitHub call is `gh api` on a repository-scoped REST path,
`repos/{owner}/{repo}/...`. gh's porcelain (`gh pr list`, `gh issue create`,
`gh repo view`, ...) goes through GraphQL, and Claude Code cloud sessions
refuse GraphQL and every REST path outside the repositories they are bound to
(`search/`, `user`, `orgs/`, ...), so a routine that used one could not run at
all. `GhApi` refuses any other path outright, and every `Gh*` class goes
through it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import quote, unquote, urlencode

from trimtab.capture.ingest import PullRequest
from trimtab.capture.written import WrittenBodyError, verify_body
from trimtab.ids import ANY_ID_SHAPE
from trimtab.dependabot import CiRun, HostError, PullRequestView
from trimtab.upstream import Issue

GH_TIMEOUT_SECONDS = 60
# REST's largest page; a list call reads pages of this size until its limit.
PER_PAGE = 100
# Closed PRs one scan of a repository's pulls reads at most; past it the scan
# is an error, not a cut-off, since a PR it did not reach could be one it needs.
SCAN_LIMIT = 1000


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
    """`gh api` on repository-scoped REST paths, never GraphQL or another REST path."""

    def __init__(self, run: Run | None = None):
        self._run = run or _subprocess_run

    def call(self, method: str, path: str, query: Mapping | None = None, body=None,
             accept: str | None = None) -> str:
        target = path.split("?")[0]
        if not target.lstrip("/").startswith("repos/"):
            raise SourceError(f"gh api {target} is not used: Claude Code cloud sessions allow only "
                              "repository-scoped REST paths (repos/{owner}/{repo}/...)")
        if any(segment in (".", "..") for segment in unquote(target).split("/")):
            # The server may normalise a dot segment, percent-encoded or not, into a path outside repos/.
            raise SourceError(f"gh api {target} is not used: a path with a . or .. segment may leave repos/")
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

    def pages(self, path: str, query: Mapping, limit: int, what: str) -> list:
        """Up to `limit` rows of a list endpoint, page by page; a short page is the last one."""
        per_page = min(PER_PAGE, limit)
        rows: list = []
        page = 1
        while len(rows) < limit:
            batch = self.json("GET", path, what, query={**query, "per_page": per_page, "page": page})
            if not isinstance(batch, list):
                raise SourceError(f"{what} returned unexpected JSON")
            rows += batch
            if len(batch) < per_page:
                break
            page += 1
        return rows[:limit]

    def all_pages(self, path: str, query: Mapping, limit: int, what: str) -> list:
        """Every row of a list endpoint, or SourceError when there are more than `limit`.

        For a list used to rule something out (a duplicate, a missing issue),
        a cut-off answer is wrong, not short: so one row past the bound is
        read, and finding it is an error rather than a silent truncation.
        """
        rows = self.pages(path, query, limit + 1, what)
        if len(rows) > limit:
            raise SourceError(f"{what} holds more than {limit} rows; refusing to work from a partial list")
        return rows


@dataclass(frozen=True)
class _ClosedPull:
    """One row of a repository's closed pulls: merged_at is "" for a PR closed without merging."""

    number: int
    body: str
    merged_at: str
    updated_at: str
    labels: frozenset[str]

    def pull(self) -> PullRequest:
        return PullRequest(self.number, self.body, self.merged_at)


def _closed_pull(r) -> _ClosedPull:
    what = "gh api pulls"
    try:
        row = _ClosedPull(number=r["number"], body=r.get("body") or "", merged_at=r["merged_at"] or "",
                          updated_at=r["updated_at"], labels=frozenset(label["name"] for label in r["labels"]))
    except (KeyError, TypeError, AttributeError) as err:
        raise SourceError(f"{what} returned unexpected JSON") from err
    if not (_is_int(row.number) and isinstance(row.body, str) and isinstance(row.merged_at, str)
            and isinstance(row.updated_at, str) and all(isinstance(n, str) for n in row.labels)):
        raise SourceError(f"{what} returned unexpected JSON")
    return row


def _within_scan(limit: int) -> None:
    if limit > SCAN_LIMIT:
        raise SourceError(f"a scan of closed PRs reads at most {SCAN_LIMIT}; lower the limit")


class GhSource:
    """Merged PRs from `repos/<r>/pulls?state=closed`, filtered here by merge time and label.

    The pulls endpoint filters by neither, so a dated read scans closed PRs
    most recently updated first and stops at the first page that reaches the
    date: a PR is updated when it merges, so none merged since the date lies
    beyond that page. The scan is bounded (SCAN_LIMIT) and refused, not cut
    off, past its bound.
    """

    def __init__(self, repo: str, run: Run | None = None):
        self.repo = repo
        self.api = GhApi(run)
        # Floor -> closed PRs updated since it. The ledger rebuild and the
        # ingest in one run read the same listing; it is fetched once (ENG-6.2).
        self._scans: dict[str, list[_ClosedPull]] = {}

    def unlabelled_merged(self, label: str, merged_since: str | None, limit: int) -> list[PullRequest]:
        # Oldest merged first (ADR 0005), so a bounded run makes progress rather
        # than re-reading the newest PRs. A backfill (no date) returns the first
        # `limit` found oldest created first, then sorted by merge time: not the
        # oldest merged overall, but each run still moves forward.
        _within_scan(limit)
        if merged_since is None:
            rows = self._oldest_unlabelled(label, limit)
        else:
            rows = [r for r in self._closed_since(merged_since)
                    if r.merged_at and r.merged_at[:10] >= merged_since and label not in r.labels]
        return [r.pull() for r in sorted(rows, key=lambda r: (r.merged_at, r.number))[:limit]]

    def labelled_merged(self, label: str, merged_since: str, limit: int) -> list[PullRequest]:
        # Newest first: the window is recent history, and a run that hits the
        # limit keeps the most recent evidence (the caller reports truncation).
        _within_scan(limit)
        rows = [r for r in self._closed_since(merged_since)
                if r.merged_at and r.merged_at[:10] >= merged_since and label in r.labels]
        return [r.pull() for r in sorted(rows, key=lambda r: (r.merged_at, r.number), reverse=True)[:limit]]

    def _page(self, order: Mapping[str, str], page: int) -> list[_ClosedPull]:
        data = self.api.json("GET", f"repos/{self.repo}/pulls", "gh api pulls",
                             query={"state": "closed", **order, "per_page": PER_PAGE, "page": page})
        if not isinstance(data, list):
            raise SourceError("gh api pulls returned unexpected JSON")
        return [_closed_pull(r) for r in data]

    def _too_many(self, since: str) -> SourceError:
        return SourceError(f"more than {SCAN_LIMIT} closed PRs on {self.repo} {since}; "
                           "refusing to work from a partial list")

    def _closed_since(self, floor: str) -> list[_ClosedPull]:
        # A wider scan already read serves a narrower floor, so the rows can
        # reach back before `floor`, unmerged ones included: every caller
        # filters by merged_at itself.
        for scanned, rows in self._scans.items():
            if scanned <= floor:
                return rows
        found: dict[int, _ClosedPull] = {}
        page = 1
        while True:
            batch = self._page({"sort": "updated", "direction": "desc"}, page)
            for r in batch:
                # A PR updated during the scan shifts the pages and can be seen twice.
                found.setdefault(r.number, r)
            if len(batch) < PER_PAGE or batch[-1].updated_at[:10] < floor:
                break
            if page * PER_PAGE >= SCAN_LIMIT:
                raise self._too_many(f"were updated since {floor}")
            page += 1
        self._scans[floor] = list(found.values())
        return self._scans[floor]

    def _oldest_unlabelled(self, label: str, limit: int) -> list[_ClosedPull]:
        """With no date, the oldest created first, until `limit` unlabelled merged PRs are found."""
        found: dict[int, _ClosedPull] = {}
        page = 1
        while True:
            batch = self._page({"sort": "created", "direction": "asc"}, page)
            for r in batch:
                if r.merged_at and label not in r.labels:
                    found.setdefault(r.number, r)
            if len(found) >= limit or len(batch) < PER_PAGE:
                return list(found.values())
            if page * PER_PAGE >= SCAN_LIMIT:
                raise SourceError(f"more than {SCAN_LIMIT} closed PRs on {self.repo} precede the oldest {limit} "
                                  f"not labelled {label}; a lower limit will not help: give the read a date "
                                  "(no --backfill, --ingest-weeks above 0)")
            page += 1

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


def _git(cwd: Path, *args: str) -> bytes:
    """git's stdout as bytes: a path need not be UTF-8."""
    try:
        done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise SourceError(f"git {args[0]}: {type(err).__name__}") from err
    if done.returncode != 0:
        # git names the paths it could not use, and a path can hold a run's ID.
        stderr = ANY_ID_SHAPE.sub("<id>", done.stderr.decode("utf-8", errors="replace")).strip()[:300]
        raise SourceError(f"git {args[0]} exited {done.returncode}: {stderr}")
    return done.stdout


GITLINK = b"160000"  # a submodule's entry: a commit, with no content of its own to read


# Added, copied, modified or type-changed against HEAD; with renames off, a
# renamed file is a deletion (nothing to read) and an addition (read).
STAGED = ("diff", "--cached", "--no-renames", "--diff-filter=ACMRT")


def staged_files(cwd: Path) -> list[tuple[int, str, bytes]]:
    """Each file the index adds, copies, modifies or changes the type of: its position in the listing of
    `git diff --cached --name-only --no-renames --diff-filter=ACMRT` (from 1, so a reader can find it
    there), its path from the repository root, and the content staged for it, which is what the commit
    records, whatever the working copy now holds.

    The raw listing gives each path with its staged mode and object, so the
    content is read by object ID: no path reaches git's messages, and no index
    lookup can miss a path (a sparse index collapses directories). Paths come
    NUL-separated, so one holding a space or a newline stays one path, and are
    decoded as the OS does (`os.fsdecode`), so any name works; a commit that
    only deletes stages nothing to read. A submodule entry has no content of
    its own: it keeps its place in the count and is not read.
    """
    root = Path(os.fsdecode(_git(cwd, "rev-parse", "--show-toplevel").strip()))
    # Pairs of `:<old mode> <new mode> <old object> <new object> <status>` and a path.
    fields = _git(root, *STAGED, "--raw", "-z").split(b"\0")
    files = []
    for position, (meta, path) in enumerate(zip(fields[0::2], fields[1::2]), 1):
        try:
            _, mode, _, obj, _ = meta.lstrip(b":").split(b" ")
        except ValueError as err:
            raise SourceError("git diff --raw returned an entry that could not be parsed") from err
        if mode != GITLINK:
            files.append((position, os.fsdecode(path), _git(root, "cat-file", "blob", obj.decode("ascii"))))
    return files


ISSUE_LIMIT = 500  # bounded: issues one label listing reads; more is an error, not a cut-off
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


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
        if closed_since is not None and not DATE.fullmatch(closed_since):
            # Both the `since` query and the closed_at comparison read it as a day.
            raise SourceError("closed_since must be a YYYY-MM-DD date")
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
        rows = self.api.all_pages(f"repos/{self.repo}/issues", {"labels": label, **filters}, ISSUE_LIMIT, what)
        return _issues_only(rows, what)

    def create(self, title: str, body: str, labels) -> str:
        """The new issue's URL, once its body reads back as the one sent (ADR 0024)."""
        data = self.api.json("POST", f"repos/{self.repo}/issues", "gh api issues",
                             body={"title": title, "body": body, "labels": list(labels)})
        if not (isinstance(data, dict) and isinstance(data.get("html_url"), str) and _is_int(data.get("number"))):
            raise SourceError("gh api issues returned unexpected JSON")
        try:
            verify_body(GhBodies(self.repo, api=self.api), "issue", data["number"], body)
        except WrittenBodyError as err:
            raise WrittenBodyError(f"{data['html_url']}: {err}; edit its body by hand") from err
        return data["html_url"]


# Where each kind of body lives, under one repository.
BODY_PATHS = {"pull": "pulls/{n}", "issue": "issues/{n}", "comment": "issues/comments/{n}"}


class GhBodies:
    """The body of a PR, an issue or a comment, read and written whole, for reading back what a run wrote."""

    def __init__(self, repo: str, run: Run | None = None, api: GhApi | None = None):
        self.repo = repo
        self.api = api or GhApi(run)

    def _path(self, kind: str, number: int) -> str:
        if kind not in BODY_PATHS or not _is_int(number):
            raise SourceError(f"no body path for {kind!r} {number!r}")
        return f"repos/{self.repo}/" + BODY_PATHS[kind].format(n=number)

    def read(self, kind: str, number: int) -> str:
        data = self.api.json("GET", self._path(kind, number), f"gh api {kind}")
        if not isinstance(data, dict) or not isinstance(data.get("body") or "", str):
            raise SourceError(f"gh api {kind} returned unexpected JSON")
        return data.get("body") or ""

    def write(self, kind: str, number: int, body: str) -> None:
        self.api.call("PATCH", self._path(kind, number), body={"body": body})

    def pull_for_head(self, branch: str) -> int:
        """The one open PR whose head is `branch` in this repository; none or several is an error."""
        owner = self.repo.split("/", 1)[0]
        rows = self.api.json("GET", f"repos/{self.repo}/pulls", "gh api pulls",
                             query={"state": "open", "head": f"{owner}:{branch}", "per_page": PER_PAGE})
        if not isinstance(rows, list) or not all(isinstance(r, dict) and _is_int(r.get("number")) for r in rows):
            raise SourceError("gh api pulls returned unexpected JSON")
        if len(rows) != 1:
            raise SourceError(f"{len(rows)} open PRs have the head {branch}; expected exactly one")
        return rows[0]["number"]


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
    rows = _issues_only(GhApi(run).all_pages(f"repos/{repo}/issues", {"labels": label, "state": "open"}, limit,
                                             what), what)
    try:
        return [Issue(int(r["number"]), r.get("title") or "", r.get("body") or "") for r in rows]
    except (ValueError, KeyError, TypeError) as err:
        raise SourceError(f"{what} returned unexpected JSON") from err
