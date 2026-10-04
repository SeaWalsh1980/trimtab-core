"""Infrastructure: where merged PRs come from. GitHub through `gh`, or a fixture file."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from trimtab.capture.ingest import PullRequest
from trimtab.upstream import Issue

GH_TIMEOUT_SECONDS = 60


class SourceError(Exception):
    """gh failed, timed out, or returned something unexpected."""


def _gh(*args: str) -> str:
    try:
        done = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise SourceError(f"gh {args[0]} {args[1]}: {type(err).__name__}") from err
    if done.returncode != 0:
        raise SourceError(f"gh {args[0]} {args[1]} exited {done.returncode}: {done.stderr.strip()[:300]}")
    return done.stdout


def _pulls(out: str) -> list[PullRequest]:
    try:
        rows = json.loads(out)
        return [PullRequest(int(r["number"]), r.get("body") or "", r.get("mergedAt") or "") for r in rows]
    except (ValueError, KeyError, TypeError) as err:
        raise SourceError("gh pr list returned unexpected JSON") from err


class GhSource:
    def __init__(self, repo: str):
        self.repo = repo

    def unlabelled_merged(self, label: str, merged_since: str | None, limit: int) -> list[PullRequest]:
        # Exact watermark query (ADR 0005), oldest first so a bounded run
        # makes progress rather than re-reading the newest PRs.
        search = f"-label:{label} sort:created-asc"
        if merged_since:
            search += f" merged:>={merged_since}"
        out = _gh("pr", "list", "--repo", self.repo, "--state", "merged", "--search", search,
                  "--json", "number,body,mergedAt", "--limit", str(limit))
        return _pulls(out)

    def labelled_merged(self, label: str, merged_since: str, limit: int) -> list[PullRequest]:
        # Newest first: the window is recent history, and a run that hits the
        # limit keeps the most recent evidence (the caller reports truncation).
        search = f"label:{label} merged:>={merged_since} sort:created-desc"
        out = _gh("pr", "list", "--repo", self.repo, "--state", "merged", "--search", search,
                  "--json", "number,body,mergedAt", "--limit", str(limit))
        return _pulls(out)

    def add_label(self, number: int, label: str) -> None:
        _gh("pr", "edit", str(number), "--repo", self.repo, "--add-label", label)

    def body(self, number: int) -> str:
        out = _gh("pr", "view", str(number), "--repo", self.repo, "--json", "body")
        return json.loads(out).get("body") or ""


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


class GhIssues:
    """Issues on one repository (Trimtab, for upstream proposals). The body goes in on stdin."""

    def __init__(self, repo: str):
        self.repo = repo
        self.destination = repo

    def recent_titles(self, label: str, closed_since: str | None) -> list[str]:
        titles = self._titles(label, "--state", "open")
        if closed_since:
            titles += self._titles(label, "--state", "closed", "--search", f"closed:>={closed_since}")
        return titles

    def _titles(self, label: str, *filters: str) -> list[str]:
        out = _gh("issue", "list", "--repo", self.repo, "--label", label, *filters,
                  "--json", "title", "--limit", "500")
        try:
            return [r["title"] for r in json.loads(out)]
        except (ValueError, KeyError, TypeError) as err:
            raise SourceError("gh issue list returned unexpected JSON") from err

    def create(self, title: str, body: str, labels) -> str:
        args = ["issue", "create", "--repo", self.repo, "--title", title, "--body-file", "-"]
        for label in labels:
            args += ["--label", label]
        return _gh_input(body, *args).strip()


def _gh_input(stdin: str, *args: str) -> str:
    try:
        done = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True,
                              timeout=GH_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise SourceError(f"gh {args[0]} {args[1]}: {type(err).__name__}") from err
    if done.returncode != 0:
        raise SourceError(f"gh {args[0]} {args[1]} exited {done.returncode}: {done.stderr.strip()[:300]}")
    return done.stdout


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

    def read(self, repo: str, path: str) -> str | None:
        try:
            done = subprocess.run(["gh", "api", f"repos/{repo}/contents/{path}",
                                   "-H", "Accept: application/vnd.github.raw"],
                                  capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as err:
            raise SourceError(f"gh api contents: {type(err).__name__}") from err
        if done.returncode == 0:
            return done.stdout
        if "404" in done.stderr or "Not Found" in done.stderr:
            return None
        raise SourceError(f"gh api contents exited {done.returncode}: {done.stderr.strip()[:300]}")


class FixtureContents:
    """Consumer files under a directory: `<dir>/<owner>/<name>/<path>`."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def read(self, repo: str, path: str) -> str | None:
        f = self.root / repo / path
        return f.read_text(encoding="utf-8") if f.is_file() else None


def open_feedback_issues(repo: str, label: str, limit: int = 500) -> list[Issue]:
    out = _gh("issue", "list", "--repo", repo, "--label", label, "--state", "open",
              "--json", "number,title,body", "--limit", str(limit))
    try:
        return [Issue(int(r["number"]), r.get("title") or "", r.get("body") or "") for r in json.loads(out)]
    except (ValueError, KeyError, TypeError) as err:
        raise SourceError("gh issue list returned unexpected JSON") from err
