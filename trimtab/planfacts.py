"""The facts the plan-run write rows read, from GitHub and git (ADR 0063 §5). Infrastructure for `planrows`.

GitHub is read through `gh api` on repository-scoped REST paths only (`GhApi`), with the gate's
own gh (`tools.gh_runner`: an empty config, the token taken once). Git is `tools.Git`: the system
binary, a built environment, no replace refs, grafts or lazy fetch. A read that fails, pages past
its limit or returns the wrong shape is a `FactsError`, which the rows turn into a refusal.

Choices the rows leave open:

- A **failing head** is a head with a run, for the branch, whose latest attempt concluded
  `failure`, `timed_out` or `cancelled`. Reading the run, not each job of each attempt, keeps the
  read to one list; an attempt that failed and was re-run to success reads as passed, which is
  AGW-4's "an infrastructure failure is re-run once and does not count".
- An **E3 facts run** is a run, for the branch, of the instance's `review.facts` workflow.
- The part's **pushes** are the branch's push events from the PR's opening. Without a `review`
  key in the instance, E12 cannot be counted and refuses.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote

from trimtab import actions
from trimtab.actions import ActionsError, Job
from trimtab.capture.ghapi import GhApi, NotFound, SourceError
from trimtab.enforcement.tools import Git, ToolError
from trimtab.instance import InstanceFile
from trimtab.planrows import Change, Facts, FactsError, Push, PrState, register_shape

SHA = re.compile(r"[0-9a-f]{40}")
ZERO = "0" * 40
ORIGIN = re.compile(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                    r"(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?")
FAILED = {"failure", "timed_out", "cancelled"}
EVENT_LIMIT = 100  # branch-activity events one count reads (REST's largest page); a full page is an error
RUN_LIMIT = 500  # workflow runs on one branch, every page
FILE_LIMIT = 2000  # changed paths one diff reads
MERGEABLE_WAITS = 3  # GitHub computes `mergeable` lazily: a null is asked again


def scan_state(runs_found: bool, jobs: Sequence[Job]) -> str:
    """The secret-scan job's state from its job records on one head: pass, pending, fail or unreadable.

    Any completed job that is not a pass (a failure, a cancellation, a skip, or a success with no
    runner or no steps) is a fail. A job still running, or no run yet, is pending. A run of the
    workflow that holds no job of that name is unreadable: the instance names a job that is not there."""
    if any(j.status == "completed" and not j.passed for j in jobs):
        return "fail"
    if any(j.status != "completed" for j in jobs):
        return "pending"
    if jobs:
        return "pass"
    return "unreadable" if runs_found else "pending"


def repository_of(url: str) -> str:
    found = ORIGIN.fullmatch(url.strip())
    if found is None:
        raise FactsError("the origin remote is not a github.com repository")
    return found["repo"]


class GitHubFacts(Facts):
    """`Facts` read from GitHub's REST API and the local checkout. Each list is read once per instance."""

    def __init__(self, repo: str, api: GhApi, git: Git, checkout: Path, instance: InstanceFile | None,
                 sleep=time.sleep):
        self.repo, self._api, self._git, self._checkout, self._instance = repo, api, git, Path(checkout), instance
        self._sleep = sleep
        self._reader = actions.Reader(api)
        self._pr: dict[str, PrState | None] = {}
        self._pulls: dict[tuple[str, int], dict | None] = {}
        self._run_rows: dict[str, list[dict]] = {}
        self._diffs: dict[tuple[str, str], tuple[Change, ...]] = {}

    # -- GitHub --------------------------------------------------------------------------

    def _json(self, path: str, what: str, query: dict | None = None, missing_ok: bool = False):
        """A read; a 404 is the caller's answer only where `missing_ok` (a branch or PR that may not exist)."""
        try:
            return self._api.json("GET", path, what, query=query)
        except NotFound as err:
            if missing_ok:
                raise
            raise FactsError(f"{what}: not found") from err
        except SourceError as err:
            raise FactsError(f"{what} could not be read") from err

    def open_pr(self, branch: str) -> PrState | None:
        if branch not in self._pr:
            owner = self.repo.split("/")[0]
            rows = self._json(f"repos/{self.repo}/pulls", "gh api pulls",
                              {"head": f"{owner}:{branch}", "state": "open", "per_page": 100})
            if not isinstance(rows, list) or len(rows) > 1:
                raise FactsError("more than one open PR, or an unexpected list, for the branch")
            state = None
            if rows:
                row = rows[0]
                number, draft, created = (row.get("number") if isinstance(row, dict) else None,
                                          row.get("draft") if isinstance(row, dict) else None,
                                          row.get("created_at") if isinstance(row, dict) else None)
                head = row.get("head") if isinstance(row, dict) else None
                if (type(number) is not int or type(draft) is not bool or not isinstance(created, str)
                        or not isinstance(head, dict) or head.get("ref") != branch):
                    raise FactsError("gh api pulls returned unexpected JSON")
                base = row.get("base")
                base_ref = base.get("ref") if isinstance(base, dict) else None
                if not isinstance(base_ref, str):
                    raise FactsError("gh api pulls returned unexpected JSON")
                state = PrState(number, draft, created, base_ref)
            self._pr[branch] = state
        return self._pr[branch]

    def _confirm_repo(self, repo: str) -> None:
        """A 404 means absent only if the repository itself answers: a token without access gets 404 for everything."""
        try:
            self._json(f"repos/{repo}", "gh api repo")
        except FactsError as err:
            raise FactsError(f"{repo} is not readable with this token") from err

    def branch_head(self, branch: str) -> str | None:
        try:
            data = self._json(f"repos/{self.repo}/git/ref/heads/{quote(branch, safe='/')}", "gh api ref", missing_ok=True)
        except NotFound:
            self._confirm_repo(self.repo)
            return None
        sha = data.get("object", {}).get("sha") if isinstance(data, dict) and isinstance(data.get("object"), dict) else None
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise FactsError("gh api ref returned unexpected JSON")
        return sha

    def secret_scan(self, head: str, branch: str) -> str:
        scan = self._instance.secret_scan if self._instance else None
        if scan is None:
            raise FactsError("the instance names no secret_scan job, so E19 cannot be shown")
        try:
            runs = [r for event in ("push", "pull_request") for r in self._reader.runs(self.repo, head, event)
                    if r.path == scan.workflow and r.head_sha == head]
            jobs = [j for r in runs for j in self._reader.jobs(self.repo, r.id)
                    if j.name == scan.job and j.head_sha == head]
        except (ActionsError, SourceError) as err:
            raise FactsError("the secret-scan job records could not be read") from err
        state = scan_state(bool(runs), jobs)
        if state == "pending" and not runs:
            seen = self._rows(branch)  # runs of other workflows but none of the scan: it never triggers here
            if seen and not any(r["path"] == scan.workflow for r in seen):
                return "unreadable"
        return state

    def _rows(self, branch: str) -> list[dict]:
        if branch not in self._run_rows:
            def read(path, what, query):
                return self._json(path, what, query)
            try:
                rows = actions.rows(read, f"repos/{self.repo}/actions/runs", "workflow_runs",
                                    "gh api actions runs", RUN_LIMIT, {"branch": branch})
            except (ActionsError, FactsError) as err:
                raise FactsError("the branch's workflow runs could not be read") from err
            for row in rows:
                if (not isinstance(row, dict) or not isinstance(row.get("head_sha"), str)
                        or not isinstance(row.get("path"), str)
                        or not (row.get("conclusion") is None or isinstance(row.get("conclusion"), str))):
                    raise FactsError("gh api actions runs returned unexpected JSON")
            self._run_rows[branch] = rows
        return self._run_rows[branch]

    def failing_heads(self, branch: str) -> set[str]:
        return {r["head_sha"] for r in self._rows(branch) if r.get("conclusion") in FAILED}

    def facts_run_heads(self, branch: str) -> set[str]:
        review = self._instance.review if self._instance else None
        if review is None:
            raise FactsError("the instance names no review key, so E12 cannot be counted")
        return {r["head_sha"] for r in self._rows(branch) if r["path"] == review.facts.workflow}

    def pushes(self, branch: str) -> Sequence[Push]:
        pr = self.open_pr(branch)
        if pr is None:
            return ()
        out: list[Push] = []
        # The endpoint pages by a cursor in the Link header (`after=`), which `gh api` does not hand us, and it
        # ignores `page=`. So one page is read, and a full one is refused rather than guessed past.
        rows = self._json(f"repos/{self.repo}/activity", "gh api activity",
                          {"ref": f"refs/heads/{branch}", "per_page": EVENT_LIMIT})
        if not isinstance(rows, list):
            raise FactsError("gh api activity returned unexpected JSON")
        if len(rows) >= EVENT_LIMIT:
            raise FactsError(f"the branch's activity holds {EVENT_LIMIT} events or more")
        for row in rows:
            before, after, stamp = (row.get(k) if isinstance(row, dict) else None
                                    for k in ("before", "after", "timestamp"))
            if not all(isinstance(v, str) for v in (before, after, stamp)):
                raise FactsError("gh api activity returned unexpected JSON")
            real = SHA.fullmatch(before) and SHA.fullmatch(after) and ZERO not in (before, after)
            if real and stamp >= pr.created:  # not a creation or a deletion
                out.append(Push(before, after))
        return out

    def conflicted(self, number: int) -> bool:
        for attempt in range(MERGEABLE_WAITS):
            data = self._json(f"repos/{self.repo}/pulls/{number}", "gh api pull")
            mergeable = data.get("mergeable") if isinstance(data, dict) else "bad"
            if mergeable is True:
                return False
            if mergeable is False:
                return True
            if mergeable is not None:
                raise FactsError("gh api pull returned unexpected JSON")
            self._sleep(1)  # null: GitHub is still computing it
        raise FactsError("the PR's mergeability is still being computed")

    # -- git -----------------------------------------------------------------------------

    def _commit(self, name: str) -> str:
        try:
            sha = self._git(self._checkout, "rev-parse", "--verify", "--end-of-options",
                            f"{name}^{{commit}}").strip()
        except ToolError as err:
            raise FactsError(str(err)) from err
        if not SHA.fullmatch(sha):
            raise FactsError("a push source is not a commit")
        return sha

    def _diff(self, old: str, new: str) -> tuple[Change, ...]:
        old, new = self._commit(old), self._commit(new)
        key = (old, new)
        if key not in self._diffs:
            flags = ("--no-ext-diff", "--no-textconv", "--no-renames", "--no-color")
            try:
                status = self._git.bytes(self._checkout, "diff", "--name-status", "-z", *flags, old, new, "--")
                patch = self._git.bytes(self._checkout, "-c", "core.quotepath=off", "diff", "-U0",
                                        "--src-prefix=a/", "--dst-prefix=b/", *flags, old, new, "--")
            except ToolError as err:
                raise FactsError(str(err)) from err
            self._diffs[key] = _changes(status, patch)
        return self._diffs[key]

    def changes(self, branch: str, source: str | None, head: str) -> Sequence[Change]:
        if not source:
            raise FactsError("the push has no source to compare with the branch")
        return self._diff(head, source)

    def register_answer(self, branch: str, before: str, after: str) -> bool:
        return register_shape(self._diff(before, after), branch)

    def _pull(self, number: int, repo: str | None) -> dict | None:
        key = (repo or self.repo, number)
        if key not in self._pulls:  # one read per PR (ENG-6.2): head and base come from the same answer
            try:
                data = self._json(f"repos/{key[0]}/pulls/{number}", "gh api pull", missing_ok=True)
            except NotFound:
                self._confirm_repo(key[0])
                data = None  # an issue, not a pull request
            self._pulls[key] = data
        return self._pulls[key]

    def _pull_ref(self, number: int, repo: str | None, side: str) -> str | None:
        data = self._pull(number, repo)
        if data is None:
            return None
        part = data.get(side) if isinstance(data, dict) else None
        ref = part.get("ref") if isinstance(part, dict) else None
        if not isinstance(ref, str):
            raise FactsError("gh api pull returned unexpected JSON")
        return ref

    def pr_head(self, number: int, repo: str | None) -> str | None:
        return self._pull_ref(number, repo, "head")

    def pr_base(self, number: int, repo: str | None) -> str | None:
        return self._pull_ref(number, repo, "base")

    def repository(self) -> str:
        return self.repo

    def local_plan_branches(self) -> Sequence[str]:
        try:
            out = self._git(self._checkout, "for-each-ref", "--format=%(refname)", "refs/heads")
        except ToolError as err:
            raise FactsError(str(err)) from err
        names = [r[len("refs/heads/"):] for r in out.splitlines() if r.startswith("refs/heads/")]
        return [n for n in names if n.casefold().startswith("plan/")]


def _changes(status: bytes, patch: bytes) -> tuple[Change, ...]:
    """Each changed path, its status, the lines it adds and how many it removes."""
    parts = status.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    if len(parts) % 2:
        raise FactsError("git diff --name-status returned an unexpected shape")
    states: dict[str, str] = {}
    for code, path in zip(parts[0::2], parts[1::2]):
        name = path.decode("utf-8", "strict")
        letter = code.decode("ascii", "strict")[:1]
        if letter not in ("A", "M", "D", "T"):
            raise FactsError(f"git diff reported {letter!r} for {name}")
        states[name] = "M" if letter == "T" else letter
        if len(states) > FILE_LIMIT:
            raise FactsError(f"the push changes more than {FILE_LIMIT} paths")
    binary: set[str] = set()
    added: dict[str, list[str]] = {n: [] for n in states}
    removed: dict[str, int] = dict.fromkeys(states, 0)
    current: str | None = None
    header = False  # from `diff --git` to the first hunk, `+++` and `---` are file names, not lines
    for raw in patch.decode("utf-8", "replace").split("\n"):
        if raw.startswith("diff --git a/"):  # a content line starts with +, - or a backslash, never this
            rest = raw[len("diff --git a/"):]
            current, header = rest[: (len(rest) - 3) // 2], True
            if current not in states:
                raise FactsError("git diff names a path --name-status did not")
        elif current is None:
            continue
        elif header:
            if raw.startswith(("Binary files", "GIT binary patch")):
                binary.add(current)
            header = not raw.startswith("@@")
        elif raw.startswith("+"):
            added[current].append(raw[1:])
        elif raw.startswith("-"):
            removed[current] += 1
    return tuple(Change(n, states[n], tuple(added[n]), removed[n], n in binary) for n in sorted(states))
