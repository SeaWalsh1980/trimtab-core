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
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from trimtab import actions, planreview, planscope
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
EVENT_LIMIT = 100  # branch-activity events per page (REST's largest)
EVENT_PAGES = 20  # pages of them one read follows through the Link cursor; past it, an error (Amendment 1)
RUN_LIMIT = 500  # workflow runs on one branch, every page
FILE_LIMIT = 2000  # changed paths one diff reads
COMMENT_LIMIT = 2000  # review comments one PR reads; past it, an error
PR_LIMIT = 2000  # pull requests in one repository, every state (§6's part numbers)
MERGEABLE_WAITS = 3  # GitHub computes `mergeable` lazily: a null is asked again
HUNK = re.compile(r"^@@ -(?P<old>[0-9]+)(,(?P<old_count>[0-9]+))? \+(?P<new>[0-9]+)(,(?P<new_count>[0-9]+))? @@")


def _when(text: str) -> datetime:
    """An RFC 3339 time with its offset, so two times compare whatever form each is written in."""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as err:
        raise FactsError("a timestamp could not be read") from err
    if moment.tzinfo is None:
        raise FactsError("a timestamp has no offset")
    return moment


def latest_attempts(jobs: Sequence[Job]) -> list[Job]:
    """The last record for each job name, as the job records list them (oldest attempt first)."""
    out: dict[str, Job] = {}
    for job in jobs:
        out[job.name] = job
    return list(out.values())


def scan_state(runs_found: bool, jobs: Sequence[Job]) -> str:
    """The secret-scan job's state from its job records on one head: pass, pending, fail or unreadable.

    Any completed job that is not a pass (a failure, a cancellation, a skip, or a success with no
    runner or no steps) is a fail. A job still running, or no run yet, is pending. A run of the
    workflow that holds no job of that name is unreadable: the instance names a job that is not there."""
    jobs = latest_attempts(jobs)
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
        self._activity: dict[str, tuple[planreview.Activity, ...]] = {}
        self._comment_rows: dict[int, tuple[tuple[planreview.Comment, ...], dict[int, str]]] = {}
        self._conflicts: dict[int, bool] = {}
        self._base_runs: dict[str, list[tuple[planreview.Review, list[planreview.Thread]]]] = {}
        self._default: str | None = None
        self._default_head: str | None = None  # the server's head for it
        self._default_local: str | None = None  # the local ref, once checked against that head
        self._branch_local: dict[str, str] = {}

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

    def activity(self, branch: str) -> tuple[planreview.Activity, ...]:
        """Every branch-activity event, oldest first, each with its actor (E12, E13).

        The endpoint pages by a cursor in a Link header and ignores `page=`, so every page is read through
        `cursor_pages` (Amendment 1 replaced the one-page read). It answers newest first; an answer not in
        that order, a deletion of the branch, or more pages than the limit is an error, so no chain is ever
        guessed past a gap."""
        if branch not in self._activity:
            try:
                rows = self._api.cursor_pages(f"repos/{self.repo}/activity", {"ref": f"refs/heads/{branch}"},
                                              EVENT_PAGES, EVENT_LIMIT, "gh api activity")
            except SourceError as err:
                raise FactsError("the branch's activity events could not be read") from err
            out: list[planreview.Activity] = []
            newer: datetime | None = None
            for row in rows:
                before, after, stamp = (row.get(k) if isinstance(row, dict) else None
                                        for k in ("before", "after", "timestamp"))
                actor = row.get("actor") if isinstance(row, dict) else None
                login = actor.get("login") if isinstance(actor, dict) else None
                if not all(isinstance(v, str) for v in (before, after, stamp, login)):
                    raise FactsError("gh api activity returned unexpected JSON")
                if not SHA.fullmatch(before) or not SHA.fullmatch(after):
                    raise FactsError("gh api activity returned a head that is not a full SHA")
                if after == ZERO:
                    raise FactsError("the branch's activity holds a deletion, so its chain cannot be read")
                when = _when(stamp)
                if newer is not None and when > newer:
                    raise FactsError("the branch's activity events are not in the order this reader expects")
                newer = when
                kind = row.get("activity_type")
                out.append(planreview.Activity(before, after, login, stamp, kind if isinstance(kind, str) else ""))
            self._activity[branch] = tuple(reversed(out))
        return self._activity[branch]

    def pushes(self, branch: str) -> Sequence[Push]:
        """The branch's pushes since its PR opened: E12 counts no event before the draft (the creation among them)."""
        pr = self.open_pr(branch)
        if pr is None:
            return ()
        opened = _when(pr.created)
        return [Push(e.before, e.after) for e in self.activity(branch)
                if ZERO not in (e.before, e.after) and _when(e.when) >= opened]

    def _comments(self, number: int) -> tuple[tuple[planreview.Comment, ...], dict[int, str]]:
        """The PR's review comments and each comment's anchor commit, from one read (ENG-6.2)."""
        if number not in self._comment_rows:
            try:
                rows = self._api.all_pages(f"repos/{self.repo}/pulls/{number}/comments", {}, COMMENT_LIMIT,
                                           "gh api review comments")
            except SourceError as err:
                raise FactsError("the PR's review comments could not be read") from err
            comments: list[planreview.Comment] = []
            commits: dict[int, str] = {}
            for row in rows:
                if not isinstance(row, dict):
                    raise FactsError("gh api review comments returned unexpected JSON")
                ident, body, commit = row.get("id"), row.get("body"), row.get("commit_id")
                user = row.get("user")
                login = user.get("login") if isinstance(user, dict) else None
                parent = row.get("in_reply_to_id")
                if type(ident) is not int or not isinstance(body, str) or not isinstance(login, str) \
                        or not isinstance(commit, str) or not SHA.fullmatch(commit) \
                        or not (parent is None or type(parent) is int):
                    raise FactsError("gh api review comments returned unexpected JSON")
                comments.append(planreview.Comment(ident, login, body, parent))
                commits[ident] = commit
            self._comment_rows[number] = (tuple(comments), commits)
        return self._comment_rows[number]

    def review_comments(self, number: int) -> tuple[planreview.Comment, ...]:
        return self._comments(number)[0]

    def comment_commits(self, number: int) -> dict[int, str]:
        return dict(self._comments(number)[1])

    def facts_runs(self, branch: str, number: int) -> list[tuple[planreview.Review, list[planreview.Thread]]]:
        """Each E3 facts run on the branch, oldest first, as `parse_facts` reads its job's log.

        Runs are ordered by id, which GitHub hands out in order for one repository. The facts job must be a
        pass with a runner and steps, as E3 requires; its log is the only source for the lines (§4). Each
        thread's `commit` is filled from the review comment the thread names, which the line does not carry,
        so E9 has an anchor to measure a fixed reply's commit against (Amendment 2)."""
        review = self._instance.review if self._instance else None
        if review is None:
            raise FactsError("the instance names no review key, so the E3 facts cannot be read")
        anchors = self.comment_commits(number)
        out = []
        for row in sorted(self._rows(branch), key=lambda r: r.get("id") or 0):
            if row["path"] != review.facts.workflow:
                continue
            if type(row.get("id")) is not int:
                raise FactsError("gh api actions runs returned a run with no id")
            try:
                jobs = [j for j in self._reader.jobs(self.repo, row["id"]) if j.name == review.facts.job]
            except (ActionsError, SourceError) as err:
                raise FactsError("the facts job's records could not be read") from err
            latest = latest_attempts(jobs)
            if len(latest) != 1 or latest[0].id is None:
                raise FactsError("a facts run holds no single job of the name the instance gives")
            if not latest[0].passed:
                continue  # a superseded or cancelled run holds no facts; a head with none refuses below
            found, threads = planreview.parse_facts(self._log(latest[0].id))
            filled = []
            for thread in threads:
                if thread.id not in anchors:
                    raise FactsError("a facts thread names a comment that is not on this PR")
                filled.append(replace(thread, commit=anchors[thread.id]))
            out.append((found, filled))
        return out

    def facts_runs_for(self, head: str) -> list[tuple[planreview.Review, list[planreview.Thread]]]:
        """Each E3 facts run on one commit, whatever branch it ran for, oldest first.

        E8's deferral needs the facts of a review whose `tree` is the merge-base's, and such a review runs
        on the default branch, never on the part's own branch, so it is read by head here and not through
        `_rows(branch)`. The threads carry no anchor commit: only their keys are compared."""
        review = self._instance.review if self._instance else None
        if review is None:
            raise FactsError("the instance names no review key, so the E3 facts cannot be read")
        if head not in self._base_runs:
            try:
                runs = [r for event in ("push", "pull_request") for r in self._reader.runs(self.repo, head, event)
                        if r.path == review.facts.workflow and r.head_sha == head]
            except (ActionsError, SourceError) as err:
                raise FactsError("the facts runs for a commit could not be read") from err
            out = []
            for run in sorted(runs, key=lambda r: r.id):
                try:
                    jobs = [j for j in self._reader.jobs(self.repo, run.id) if j.name == review.facts.job]
                except (ActionsError, SourceError) as err:
                    raise FactsError("the facts job's records could not be read") from err
                latest = latest_attempts(jobs)
                if len(latest) != 1 or latest[0].id is None:
                    raise FactsError("a facts run holds no single job of the name the instance gives")
                if not latest[0].passed:
                    continue  # a superseded or cancelled run holds no facts to read
                out.append(planreview.parse_facts(self._log(latest[0].id)))
            self._base_runs[head] = out
        return self._base_runs[head]

    def _log(self, job_id: int) -> str:
        """One job's log, as text. Only the fixed lines of §4 are read from it, by `planreview.parse_facts`."""
        try:
            return self._api.call("GET", f"repos/{self.repo}/actions/jobs/{job_id}/logs")
        except SourceError as err:
            raise FactsError("the facts job's log could not be read") from err

    def commit_of(self, rev: str) -> str:
        """The commit a revision names, as a SHA: the ledger records a push by its commits, never by a name."""
        return self._commit(rev)

    def default_branch(self) -> str:
        if self._default is None:
            data = self._json(f"repos/{self.repo}", "gh api repo")
            name = data.get("default_branch") if isinstance(data, dict) else None
            if not isinstance(name, str) or not name:
                raise FactsError("gh api repo returned no default branch")
            self._default = name
        return self._default

    def conflicted(self, number: int) -> bool:
        if number not in self._conflicts:  # one definite answer per PR, however many rows ask (ENG-6.2)
            self._conflicts[number] = self._conflicted(number)
        return self._conflicts[number]

    def _conflicted(self, number: int) -> bool:
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

    def _text(self, *args: str) -> str:
        try:
            return self._git(self._checkout, *args)
        except ToolError as err:
            raise FactsError(str(err)) from err

    def _ancestor(self, older: str, newer: str) -> bool:
        """Whether `older` is `newer` or one of its ancestors, asked as a read rather than an exit code."""
        return self._text("merge-base", "--end-of-options", older, newer).strip() == self._commit(older)

    def default_head(self) -> str:
        """The default branch's head as the server has it, which every local ref is checked against."""
        if self._default_head is None:
            data = self._json(f"repos/{self.repo}/branches/{quote(self.default_branch(), safe='')}",
                              "gh api branch")
            commit = data.get("commit") if isinstance(data, dict) else None
            sha = commit.get("sha") if isinstance(commit, dict) else None
            if not isinstance(sha, str) or not SHA.fullmatch(sha):
                raise FactsError("gh api branch returned no head for the default branch")
            self._default_head = sha
        return self._default_head

    def _verified(self, ref: str, server: str, what: str) -> str:
        """A local remote-tracking ref, checked against the head the server reports for it.

        Nothing refuses a session's own `git update-ref`, so a ref read without this check is a fact the
        agent can write: a moved `origin/<default>` would move every merge-base, tree, diff and plan text
        the rows rest on (ADR 0063 §5, SEC-2.5)."""
        local = self._text("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").strip()
        if not SHA.fullmatch(local):
            raise FactsError(f"{what} could not be read locally")
        if local != server:
            raise FactsError(f"{what} is not the head the server reports; fetch before judging a plan run")
        return local

    def default_sha(self) -> str:
        """`origin/<default>`, verified against the server (every row that reads it goes through here)."""
        if self._default_local is None:
            self._default_local = self._verified(f"origin/{self.default_branch()}", self.default_head(),
                                                 "the default branch's local ref")
        return self._default_local

    def branch_sha(self, branch: str) -> str:
        """`origin/<branch>`, verified against the server."""
        if branch not in self._branch_local:
            head = self.branch_head(branch)
            if head is None:
                raise FactsError("the branch is not on the remote, so its local ref vouches for nothing")
            self._branch_local[branch] = self._verified(f"origin/{branch}", head, "the branch's local ref")
        return self._branch_local[branch]

    def merge_base_of(self, rev: str) -> str:
        """The merge-base of one revision with the default branch as the remote has it."""
        base = self._text("merge-base", "--end-of-options", self.default_sha(), rev).strip()
        if not SHA.fullmatch(base):
            raise FactsError("the merge-base with the default branch could not be read")
        return base

    def merge_base(self, branch: str) -> str:
        """The merge-base of the branch with the default branch, both as the remote has them (E8, E13).

        A branch that is not on the remote has no merge-base to read here; a caller that must judge the
        push creating it asks `merge_base_of` for the commit being sent instead."""
        return self.merge_base_of(self.branch_sha(branch))

    def parents(self, rev: str) -> tuple[str, ...]:
        """A commit's parents, as git lists them, so §5a can tell a merge of the default branch."""
        out = self._text("rev-list", "--parents", "-n", "1", "--end-of-options", rev).split()
        if not out or any(not SHA.fullmatch(c) for c in out):
            raise FactsError("a commit's parents could not be read")
        return tuple(out[1:])

    def change_from(self, base: str, source: str) -> Sequence[Change]:
        """The change `source` makes against `base`: E18's own-change for a push that creates the branch,
        and for a merge of the default branch its diff from the default-branch parent (§5a)."""
        return self._diff(base, source)

    def tree_of(self, commit: str) -> str:
        """A commit's tree, which E8 compares with the tree an E2 run reviewed."""
        sha = self._text("rev-parse", "--verify", "--end-of-options", f"{commit}^{{tree}}").strip()
        if not SHA.fullmatch(sha):
            raise FactsError("a commit's tree could not be read")
        return sha

    def branch_commits(self, branch: str) -> tuple[str, ...]:
        """`origin/<default>..origin/<branch>`, oldest first: the commits E13 must find inside ledger
        entries. Both refs are the verified ones, never the names, so a local ref write moves nothing."""
        out = self._text("rev-list", "--reverse", "--end-of-options",
                         f"{self.default_sha()}..{self.branch_sha(branch)}").split()
        if any(not SHA.fullmatch(c) for c in out):
            raise FactsError("the branch's commits could not be read")
        return tuple(out)

    def push_commits(self, old: str, new: str, merge_base: str) -> frozenset[str]:
        """The commits one ledger push introduced. A push that created the branch (`old` all zeros) is bounded
        by the merge-base, so the default branch's own history is never counted as the run's (Amendment 2)."""
        start = merge_base if old == ZERO else old
        out = self._text("rev-list", "--end-of-options", f"{start}..{new}").split()
        if any(not SHA.fullmatch(c) for c in out):
            raise FactsError("a push's commits could not be read")
        return frozenset(out)

    def line_text(self, commit: str, path: str, line: int) -> str | None:
        """The anchored line at the reviewed commit, for E8's key, or None where the path or line is not there."""
        try:
            blob = self._git.bytes(self._checkout, "show", "--end-of-options", f"{commit}:{path}")
        except ToolError:
            return None  # a path the reviewed commit does not hold: the key then matches nothing
        lines = blob.decode("utf-8", "replace").split("\n")
        return lines[line - 1] if 1 <= line <= len(lines) else None

    def on_default(self, sha: str) -> bool:
        """Whether a commit is on the default branch, so E13 reads it as none of the run's work."""
        return self._ancestor(sha, self.default_sha())

    def _hunks(self, *args: str) -> tuple[tuple[int, int], tuple[int, int]]:
        """The old-side and new-side line ranges of each hunk a diff holds, as (start, end) pairs."""
        old: list[tuple[int, int]] = []
        new: list[tuple[int, int]] = []
        for raw in self._text(*args).split("\n"):
            found = HUNK.match(raw)
            if found is None:
                continue
            for side, rows in (("old", old), ("new", new)):
                start = int(found[side])
                count = int(found[f"{side}_count"]) if found[f"{side}_count"] is not None else 1
                if count:
                    rows.append((start, start + count - 1))
        return tuple(old), tuple(new)

    def outside_diff(self, branch: str, head: str, path: str, line: int) -> bool:
        """Whether a finding's line lies outside the head side of the diff from the merge-base (E8's deferral)."""
        _, new = self._hunks("diff", "-U0", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
                             f"{self.merge_base(branch)}..{head}", "--", path)
        return not any(start <= line <= end for start, end in new)

    def fix_valid(self, branch: str, thread: planreview.Thread, sha: str) -> bool:
        """E9: the cited commit descends from the thread's commit, is on the branch, and changes a line in
        the thread's hunk. The hunk is read as the commit's own change to the thread's path covering the
        anchored line; nothing but the line number is in the E3 facts, so that is the measure."""
        self._commit(sha)  # an unreadable commit is a failed read, not a `False`
        if not self._ancestor(thread.commit, sha) or not self._ancestor(sha, self.branch_sha(branch)):
            return False
        old, new = self._hunks("show", "-U0", "--first-parent", "--format=", "--no-ext-diff", "--no-textconv",
                               "--no-color", "--no-renames", "--end-of-options", sha, "--", thread.path)
        return any(start <= thread.line <= end for start, end in (*old, *new))

    def progress_names(self, plan: str) -> tuple[str, ...]:
        """The plan's progress-record paths on the default branch, which §6 reads for the highest part."""
        folder = f"{planscope.PLANS_DIR}/progress/{plan}/"
        out = self._text("ls-tree", "-r", "--name-only", "--end-of-options",
                         self.default_sha(), "--", folder)
        return tuple(name for name in out.split("\n") if name)

    def pr_head_refs(self) -> tuple[str, ...]:
        """The head ref of every PR in the repository, open or closed (§6: a closed PR keeps its branch name
        where a branch-activity event has expired)."""
        try:
            rows = self._api.all_pages(f"repos/{self.repo}/pulls", {"state": "all"}, PR_LIMIT,
                                       "gh api pulls (every state)")
        except SourceError as err:
            raise FactsError("the repository's pull requests could not be read") from err
        out = []
        for row in rows:
            head = row.get("head") if isinstance(row, dict) else None
            ref = head.get("ref") if isinstance(head, dict) else None
            if not isinstance(ref, str):
                raise FactsError("gh api pulls returned unexpected JSON")
            out.append(ref)
        return tuple(out)

    def last_answer(self, branch: str) -> str | None:
        """The last commit answering a CI review or a failing check (E10): the head a push whose `before` was
        a failing or reviewed head wrote. None where no push answers one."""
        answered = self.failing_heads(branch) | self.facts_run_heads(branch)
        found = [p.after for p in self.pushes(branch) if p.before in answered]
        return found[-1] if found else None

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
    gone: dict[str, list[str]] = {n: [] for n in states}  # the removed lines' text (E18 judges it)
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
            gone[current].append(raw[1:])
    return tuple(Change(n, states[n], tuple(added[n]), removed[n], n in binary, tuple(gone[n]))
                 for n in sorted(states))
