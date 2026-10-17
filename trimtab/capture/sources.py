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

import contextlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping
from urllib.parse import quote, unquote, urlencode

from trimtab.capture.ingest import PullRequest
from trimtab.capture.written import SETTLE_SECONDS, WrittenBodyError, verify_bodies
from trimtab.ids import ANY_ID_SHAPE
from trimtab.dependabot import CiRun, HostError, PullRequestView
from trimtab.propose import Filer, PullTitle
from trimtab.upstream import Issue

GH_TIMEOUT_SECONDS = 60
# REST's largest page; a list call reads pages of this size until its limit.
PER_PAGE = 100
# Closed PRs one scan of a repository's pulls reads at most; past it the scan
# is an error, not a cut-off, since a PR it did not reach could be one it needs.
SCAN_LIMIT = 1000


class SourceError(HostError):
    """gh failed, timed out, or returned something unexpected."""


class Refused(SourceError):
    """gh api answered a 4xx: GitHub refused the request, so it changed nothing."""


class NotFound(Refused):
    """gh api answered 404."""


CLIENT_ERROR = re.compile(r"HTTP 4\d\d\b")


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
            if CLIENT_ERROR.search(done.stderr):
                raise Refused(message)
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
class _PullRow:
    """One row of a repository's pulls, open or closed: merged_at is "" unless it merged."""

    number: int
    body: str
    merged_at: str
    updated_at: str
    labels: frozenset[str]
    title: str = ""
    closed_at: str = ""

    def pull(self) -> PullRequest:
        return PullRequest(self.number, self.body, self.merged_at)


def _pull_row(r) -> _PullRow:
    """The one parser for a `pulls` row, open or closed: a row that does not have the expected shape is refused."""
    what = "gh api pulls"
    try:
        row = _PullRow(number=r["number"], body=r.get("body") or "", merged_at=r["merged_at"] or "",
                          updated_at=r["updated_at"], labels=frozenset(label["name"] for label in r["labels"]),
                          title=r["title"], closed_at=r.get("closed_at") or "")
    except (KeyError, TypeError, AttributeError) as err:
        raise SourceError(f"{what} returned unexpected JSON") from err
    if not (_is_int(row.number) and isinstance(row.body, str) and isinstance(row.merged_at, str)
            and isinstance(row.updated_at, str) and all(isinstance(n, str) for n in row.labels)
            and isinstance(row.title, str) and isinstance(row.closed_at, str)):
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
        # The closed PRs' pages read so far, most recently updated first. The
        # ledger rebuild, the ingest and the PR check in one run read the same
        # listing: an earlier date continues from the last page read rather
        # than starting again, so each page is fetched once (ENG-6.2).
        self._pages: list[list[_PullRow]] = []
        self._exhausted = False

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

    def recent_pulls(self, closed_since: str | None) -> list[PullTitle]:
        """Open PRs, and closed ones updated since `closed_since`, from the closed-PR pages ingest reads: a page
        already read is not fetched again."""
        what = "gh api pulls"
        rows = self.api.all_pages(f"repos/{self.repo}/pulls", {"state": "open"}, SCAN_LIMIT, what)
        found = [PullTitle(r.number, r.title, "open") for r in map(_pull_row, rows)]
        if closed_since:
            found += [PullTitle(r.number, r.title, "merged" if r.merged_at else "closed", r.merged_at, r.closed_at)
                      for r in self._closed_since(closed_since) if r.updated_at[:10] >= closed_since]
        return found

    def _page(self, order: Mapping[str, str], page: int) -> list[_PullRow]:
        data = self.api.json("GET", f"repos/{self.repo}/pulls", "gh api pulls",
                             query={"state": "closed", **order, "per_page": PER_PAGE, "page": page})
        if not isinstance(data, list):
            raise SourceError("gh api pulls returned unexpected JSON")
        return [_pull_row(r) for r in data]

    def _too_many(self, since: str) -> SourceError:
        return SourceError(f"more than {SCAN_LIMIT} closed PRs on {self.repo} {since}; "
                           "refusing to work from a partial list")

    def _closed_since(self, floor: str) -> list[_PullRow]:
        # The rows can reach back before `floor`, unmerged ones included:
        # every caller filters by merged_at or updated_at itself.
        def reached() -> bool:
            return self._exhausted or bool(self._pages and self._pages[-1]
                                           and self._pages[-1][-1].updated_at[:10] < floor)

        while not reached():
            if len(self._pages) * PER_PAGE >= SCAN_LIMIT:
                raise self._too_many(f"were updated since {floor}")
            batch = self._page({"sort": "updated", "direction": "desc"}, len(self._pages) + 1)
            self._pages.append(batch)
            if len(batch) < PER_PAGE:
                self._exhausted = True
        found: dict[int, _PullRow] = {}
        for batch in self._pages:
            for r in batch:
                # A PR updated during the scan shifts the pages and can be seen twice.
                found.setdefault(r.number, r)
        return list(found.values())

    def _oldest_unlabelled(self, label: str, limit: int) -> list[_PullRow]:
        """With no date, the oldest created first, until `limit` unlabelled merged PRs are found."""
        found: dict[int, _PullRow] = {}
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

    @staticmethod
    def _merged(r: dict) -> bool:
        """A row marked open is open, whatever else it carries: never merged evidence."""
        return bool(r.get("merged_at")) and r.get("state") != "open"

    def unlabelled_merged(self, label: str, merged_since: str | None, limit: int) -> list[PullRequest]:
        rows = sorted(self._rows(), key=lambda r: r["number"])
        todo = [r for r in rows if self._merged(r) and label not in r.get("labels", [])
                and (merged_since is None or r["merged_at"][:10] >= merged_since)]
        return [PullRequest(r["number"], r.get("body", ""), r["merged_at"]) for r in todo[:limit]]

    def labelled_merged(self, label: str, merged_since: str, limit: int) -> list[PullRequest]:
        rows = sorted(self._rows(), key=lambda r: r["number"], reverse=True)
        done = [r for r in rows if self._merged(r) and label in r.get("labels", [])
                and r["merged_at"][:10] >= merged_since]
        return [PullRequest(r["number"], r.get("body", ""), r["merged_at"]) for r in done[:limit]]

    def recent_pulls(self, closed_since: str | None) -> list[PullTitle]:
        """Rows with `"state": "open"` are open; the rest are closed, merged when `merged_at` is set, and dated by
        `updated_at`, else `merged_at`."""
        def state(r):
            return "open" if r.get("state") == "open" else "merged" if self._merged(r) else "closed"

        def recent(r):
            return state(r) == "open" or (closed_since is not None
                                          and (r.get("updated_at") or r.get("merged_at") or "")[:10] >= closed_since)
        rows = [r for r in self._rows() if recent(r)]
        if not all(isinstance(r.get("title"), str) for r in rows):
            raise SourceError("fixture pulls: a PR row has no title")  # GitHub's rows always carry one
        return [PullTitle(r["number"], r["title"], state(r), r["merged_at"] if self._merged(r) else "",
                          r.get("closed_at") or "") for r in rows]

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

    def __init__(self, repo: str, run: Run | None = None, settle_seconds: float = SETTLE_SECONDS):
        self.repo = repo
        self.destination = repo
        self.api = GhApi(run)
        self.settle_seconds = settle_seconds

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

    @contextlib.contextmanager
    def filing(self) -> Iterator[Filer]:
        """Issues are filed only inside this block, and every one filed is read back on leaving it, on success
        or error (ADR 0024): a caller cannot file an issue and skip the read-back. An interrupt (Ctrl-C) is not
        held up by the wait: it goes on at once, with a note naming each issue left to check by hand."""
        created: list[tuple[str, int, str]] = []

        def create(title: str, body: str, labels) -> str:
            url, number = self._file(title, body, labels)
            created.append((url, number, body))
            return url

        try:
            yield Filer(create)
        except Exception:
            self._read_back(created)
            raise
        except BaseException as interrupt:
            if created:
                interrupt.add_note(f"interrupted before the read-back; check each with `trimtab verify-body "
                                   f"--repo {self.repo} --issue N`: {', '.join(url for url, _, _ in created)}")
            raise
        self._read_back(created)

    def _file(self, title: str, body: str, labels) -> tuple[str, int]:
        """File one issue: its URL and number.

        A refusal (4xx) changed nothing, so a re-run is safe: SourceError. Any other failure may have filed
        the issue, which a re-run would skip and never read back: WrittenBodyError, naming its title.
        """
        maybe = (f"the issue titled {title!r} may have been filed and was not read back; check the open "
                 f"issues on {self.repo} by hand before a re-run")
        try:
            data = self.api.json("POST", f"repos/{self.repo}/issues", "gh api issues",
                                 body={"title": title, "body": body, "labels": list(labels)})
        except Refused:
            raise
        except SourceError as err:
            raise WrittenBodyError(f"{maybe} ({err})") from err
        if not (isinstance(data, dict) and isinstance(data.get("html_url"), str) and _is_int(data.get("number"))):
            raise WrittenBodyError(f"{maybe} (gh api issues returned unexpected JSON)")
        return data["html_url"], data["number"]

    def _read_back(self, created: list[tuple[str, int, str]]) -> None:
        """The issues filed in one `filing` block, read back after one shared wait (ADR 0024)."""
        urls = {number: url for url, number, _ in created}
        try:
            verify_bodies(GhBodies(self.repo, api=self.api), [("issue", n, b) for _, n, b in created],
                          settle_seconds=self.settle_seconds, sleep=time.sleep)
        except WrittenBodyError as err:
            named = ", ".join(urls[number] for _, number in err.targets)
            raise WrittenBodyError(f"{err}; edit each by hand: {named}", err.targets) from err
        except SourceError as err:
            # The issues exist, so a re-run skips them and never reads them back: stop here, naming them.
            raise WrittenBodyError(f"created, but not all read back ({err}); check each with `trimtab verify-body "
                                   f"--repo {self.repo} --issue N`: {', '.join(urls.values())}") from err


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
        rows = [r for r in self._rows() if label in r.get("labels", []) and recent(r)]
        if not all(isinstance(r.get("title"), str) for r in rows):
            raise SourceError("fixture issues: an issue row has no title")  # as GhIssues refuses one
        return [r["title"] for r in rows]

    def create(self, title: str, body: str, labels) -> str:
        rows = self._rows()
        rows.append({"title": title, "body": body, "labels": list(labels), "state": "open"})
        self.path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        return f"fixture issue {len(rows)}"

    @contextlib.contextmanager
    def filing(self) -> Iterator[Filer]:
        """A file holds what was written: nothing to read back."""
        yield Filer(self.create)


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
