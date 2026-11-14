"""`gh api` on repository-scoped REST paths, and the errors it raises. Standard library only.

Split out of `capture/sources.py` so a hook can read GitHub without importing the capture package,
whose other modules need PyYAML: a hook runs the interpreter isolated (`-I`, and for some `-S`), where
a package installed for the user is not found. `capture/sources.py` re-exports every name here.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from trimtab.ids import ANY_ID_SHAPE


class HostError(Exception):
    """The host (GitHub) failed, timed out, or answered with something unexpected."""


GH_TIMEOUT_SECONDS = 60
GIT_TIMEOUT_SECONDS = 30
# REST's largest page; a list call reads pages of this size until its limit.
PER_PAGE = 100


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


LINK_ENTRY = re.compile(r"<([^>]*)>([^<]*)")
LINK_URL = re.compile(r"<[^>]*>")
LINK_REL = re.compile(r'rel\s*=\s*"?([^";,]*)"?')


def _next_cursor(link: str | None, what: str) -> str | None:
    """The `after` cursor of a Link header's rel="next", None when there is no next page.

    The word `next` anywhere in a header that holds no readable next entry is an error, not an end: a
    form this reader does not know must not read as the last page."""
    if not link:
        return None
    target = None
    for url, params in LINK_ENTRY.findall(link):
        rel = LINK_REL.search(params)
        if rel and "next" in rel.group(1).casefold().split():
            target = url
    if target is None:
        # `next` outside every <url> (a rel in a form not read), never inside one: a repository named `nextjs-x`
        # must not make the last page an error.
        if "next" in LINK_URL.sub("", link).casefold():
            raise SourceError(f"{what} has a Link header this reader cannot place; refusing a partial list")
        return None
    values = parse_qs(urlsplit(target).query).get("after")
    if not values or not values[0]:
        raise SourceError(f"{what} has a next page with no cursor; refusing to page by number")
    return values[0]


default_run = _subprocess_run  # the runner GhApi uses when given none, for a caller that wraps it (a memo)


class GhApi:
    """`gh api` on repository-scoped REST paths, never GraphQL or another REST path."""

    def __init__(self, run: Run | None = None):
        self._run = run or _subprocess_run

    def call(self, method: str, path: str, query: Mapping | None = None, body=None,
             accept: str | None = None, include: bool = False) -> str:
        target = path.split("?")[0]
        if not target.lstrip("/").startswith("repos/"):
            raise SourceError(f"gh api {target} is not used: Claude Code cloud sessions allow only "
                              "repository-scoped REST paths (repos/{owner}/{repo}/...)")
        if any(segment in (".", "..") for segment in unquote(target).split("/")):
            # The server may normalise a dot segment, percent-encoded or not, into a path outside repos/.
            raise SourceError(f"gh api {target} is not used: a path with a . or .. segment may leave repos/")
        argv = ["gh", "api", "-X", method, path + (f"?{urlencode(query)}" if query else "")]
        if include:
            argv += ["-i"]
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

    def call_with_headers(self, method: str, path: str, query: Mapping | None = None) -> tuple[dict, str]:
        """(response headers with lower-cased names, body) of a call made with `gh api -i`."""
        out = self.call(method, path, query=query, include=True)
        head, sep, body = out.replace("\r\n", "\n").partition("\n\n")
        lines = head.split("\n")
        if not sep or not lines or not lines[0].startswith("HTTP/"):
            raise SourceError(f"gh api {method} {path.split('?')[0]} returned no header block")
        headers = {}
        for line in lines[1:]:
            name, colon, value = line.partition(":")
            if colon:
                key = name.strip().lower()
                headers[key] = f"{headers[key]}, {value.strip()}" if key in headers else value.strip()
        return headers, body

    def cursor_pages(self, path: str, query: Mapping, page_limit: int, per_page: int, what: str) -> list:
        """Every row of a list endpoint that pages by a Link-header cursor (`after=`), at most `page_limit`
        pages; more to read at the limit is an error, never a cut-off list. `page=` is never sent."""
        if page_limit < 1 or {"page", "after", "per_page"} & set(query):
            raise SourceError(f"{what}: a cursor read takes a page limit of at least 1 and sets its own paging")
        rows: list = []
        after = None
        for _ in range(page_limit):
            q = {**query, "per_page": per_page, **({"after": after} if after else {})}
            headers, body = self.call_with_headers("GET", path, q)
            try:
                batch = json.loads(body)
            except ValueError as err:
                raise SourceError(f"{what} returned unexpected JSON") from err
            if not isinstance(batch, list):
                raise SourceError(f"{what} returned unexpected JSON")
            rows += batch
            after = _next_cursor(headers.get("link"), what)
            if after is None:
                return rows
        raise SourceError(f"{what} holds more than {page_limit} pages; refusing to work from a partial list")

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


def git_bytes(cwd: Path | str, *args: str, env: dict[str, str] | None = None, stdin: bytes | None = None,
              name: str | None = None) -> bytes:
    """git's stdout as bytes: a path need not be UTF-8. env replaces the inherited environment when given. name is
    the subcommand errors report, for a caller whose args open with `-c` options."""
    return git_answer(cwd, *args, env=env, stdin=stdin, name=name)[1]


def git_answer(cwd: Path | str, *args: str, env: dict[str, str] | None = None, stdin: bytes | None = None,
               name: str | None = None, answers: tuple[int, ...] = (0,)) -> tuple[int, bytes]:
    """git's exit code and stdout, for a command whose exit code is part of its answer (`merge-base` exits 1 for
    no common ancestor, `merge-tree` 1 for a conflict). Any code outside `answers` raises, as in `git_bytes`."""
    name = name or args[0]
    try:
        done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, timeout=GIT_TIMEOUT_SECONDS,
                              env=env, input=stdin)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise SourceError(f"git {name}: {type(err).__name__}") from err
    if done.returncode not in answers:
        # git names the paths it could not use, and a path can hold a run's ID.
        stderr = ANY_ID_SHAPE.sub("<id>", done.stderr.decode("utf-8", errors="replace")).strip()[:300]
        raise SourceError(f"git {name} exited {done.returncode}: {stderr}")
    return done.returncode, done.stdout
