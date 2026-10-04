"""The harness block for a Dependabot PR: Dependabot writes its own body and cannot be templated.

An instance whose CI runs `check-pr` fails every Dependabot PR until the block
is added. This module decides what to write and what to re-run; it never calls
GitHub itself. Side effects go through a `PullRequestHost`, and are gated like
`ingest` and `bump`: `plan` is a dry run, and its confirmation token is a
digest of exactly what would be written.

PR bodies are untrusted data. Dependabot's text is kept byte for byte as the
prefix of the new body, is parsed only by `check_body`, and no message here
quotes it, nor the author's login.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Iterable, Mapping, Protocol

from trimtab.capture.prblock import APPLIED, COST, FEEDBACK, SECTIONS, check_body
from trimtab.items import Item, Problem

# What `gh pr view --json author` reports for Dependabot; REST says `dependabot[bot]`.
DEPENDABOT_LOGIN = "app/dependabot"
# GitHub refuses a PR body longer than this many characters.
MAX_BODY = 65536
# Runs of one workflow at one head SHA: a handful at most, so this bound is never reached in practice.
RUN_LIMIT = 10
SEPARATOR = "\n\n"
# A run conclusion is shown only when it looks like one of GitHub's (`cancelled`, `timed_out`).
CONCLUSION = re.compile(r"[a-z_]{1,40}")
BLOCK = f"""## {APPLIED}
```yaml
- id: none
  why: automated dependency bump opened by Dependabot; block added by trimtab dependabot-block; the operator reviews this PR
```

## {FEEDBACK}
```yaml
[]
```

## {COST}
```yaml
{{}}
```
"""


class HostError(Exception):
    """The host (GitHub) failed, timed out, or answered with something unexpected."""


class NotDependabot(Exception):
    """The PR was not opened by Dependabot, or is not open."""


class BlockRefused(Exception):
    """The body cannot take the block: adding it would not give a body that passes `check_body`."""

    def __init__(self, message: str, problems: tuple[Problem, ...] = ()):
        super().__init__(message)
        self.problems = problems


class StaleBlockPlan(Exception):
    """The PR or its runs changed since the dry run printed the token."""


class ReadBackFailed(Exception):
    """The body was written, but reading it back does not give a valid block."""


class RerunFailed(Exception):
    """The body step finished (or had nothing to do), but the re-run did not start."""

    def __init__(self, message: str, wrote_body: bool):
        super().__init__(message)
        self.wrote_body = wrote_body


@dataclass(frozen=True)
class PullRequestView:
    number: int
    author_login: str
    author_is_bot: bool
    body: str
    head_sha: str
    state: str


@dataclass(frozen=True)
class CiRun:
    id: int
    status: str
    conclusion: str


class PullRequestHost(Protocol):
    """One repository's PRs and CI runs. Every method raises HostError when the host fails."""

    def view(self, number: int) -> PullRequestView: ...

    def set_body(self, number: int, body: str) -> None: ...

    def runs(self, head_sha: str, workflow: str, limit: int) -> list[CiRun]: ...

    def rerun_failed(self, run_id: int) -> None: ...


@dataclass(frozen=True)
class BlockPlan:
    repo: str
    number: int
    head_sha: str
    workflow: str
    old_length: int
    old_digest: str
    new_body: str | None
    rerun_run_id: int | None
    # The newest run of the workflow at the head, or None when there is none.
    newest_run: CiRun | None = None

    @property
    def nothing_to_do(self) -> bool:
        return self.new_body is None and self.rerun_run_id is None

    @property
    def run_in_progress(self) -> bool:
        return self.newest_run is not None and self.newest_run.status != "completed"

    @property
    def ci_needs_attention(self) -> bool:
        """No run at the head, or a newest run that ended neither passed nor failed (cancelled, timed out).

        Neither is re-run, and neither is fixed by the block alone, so the operator has to look.
        """
        newest = self.newest_run
        if newest is None:
            return True
        return newest.status == "completed" and newest.conclusion not in ("success", "failure")

    @property
    def shown_conclusion(self) -> str:
        """The newest run's conclusion, safe to print: GitHub's word, or a neutral stand-in."""
        value = self.newest_run.conclusion if self.newest_run else ""
        return value if CONCLUSION.fullmatch(value) else "an unexpected conclusion"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _missing_only(problems: Iterable[Problem]) -> bool:
    """True when the body has no harness block at all: every section is missing and nothing else is wrong."""
    found = {(p.code, p.where) for p in problems}
    return found == {("missing-section", name) for name in SECTIONS}


def _new_body(body: str, registry: Mapping[str, Item]) -> str | None:
    """The body with the block appended, or None when a valid block is already there."""
    current = check_body(body, registry)
    if current.ok:
        return None
    if not _missing_only(current.problems):
        raise BlockRefused("the body already holds an invalid harness block; fix it by hand", current.problems)
    new = body.rstrip() + SEPARATOR + BLOCK
    if len(new) > MAX_BODY:
        raise BlockRefused(f"the body with the block would exceed GitHub's {MAX_BODY}-character limit")
    after = check_body(new, registry)
    if not after.ok:
        # An unclosed fence or a version marker in Dependabot's text would hide the block.
        raise BlockRefused("the body with the block appended still fails check-pr", after.problems)
    return new


def plan(pr: PullRequestView, runs: Iterable[CiRun], registry: Mapping[str, Item], repo: str,
         workflow: str) -> BlockPlan:
    """What to write and what to re-run. Changes nothing."""
    if not (pr.author_login == DEPENDABOT_LOGIN and pr.author_is_bot is True):
        raise NotDependabot(f"PR #{pr.number} was not opened by Dependabot")
    if pr.state != "OPEN":
        raise NotDependabot(f"PR #{pr.number} is not open")
    new = _new_body(pr.body, registry)
    newest = max(runs, key=lambda r: r.id, default=None)
    rerun = newest.id if newest and newest.status == "completed" and newest.conclusion == "failure" else None
    return BlockPlan(repo=repo, number=pr.number, head_sha=pr.head_sha, workflow=workflow,
                     old_length=len(pr.body), old_digest=_digest(pr.body), new_body=new,
                     rerun_run_id=rerun, newest_run=newest)


def confirmation(todo: BlockPlan) -> str:
    """A short digest of exactly what `apply` will write and re-run."""
    payload = json.dumps(asdict(todo), sort_keys=True)
    return _digest(payload)[:12]


@dataclass(frozen=True)
class Applied:
    wrote_body: bool
    reran: int | None


def read_plan(host: PullRequestHost, registry: Mapping[str, Item], repo: str, workflow: str,
              number: int) -> BlockPlan:
    """Read the PR and its runs, and plan. The dry run and `apply` both come through here."""
    pr = host.view(number)
    return plan(pr, host.runs(pr.head_sha, workflow, RUN_LIMIT), registry, repo, workflow)


def apply(host: PullRequestHost, registry: Mapping[str, Item], repo: str, workflow: str, number: int,
          confirm: str) -> Applied:
    """Re-read, check the token, write the body, read it back, re-run. Safe to run again after any failure."""
    todo = read_plan(host, registry, repo, workflow, number)
    if confirm != confirmation(todo):
        raise StaleBlockPlan("the PR or its runs changed since the dry run; run the dry run again")
    if todo.new_body is not None:
        host.set_body(number, todo.new_body)
        try:
            written = host.view(number)
        except HostError as err:
            raise ReadBackFailed(f"PR #{number}'s body was written but could not be read back") from err
        if not check_body(written.body, registry).ok:
            raise ReadBackFailed(f"PR #{number}'s body was written but does not read back with a valid block")
    if todo.rerun_run_id is not None:
        try:
            host.rerun_failed(todo.rerun_run_id)
        except HostError as err:
            raise RerunFailed(f"run {todo.rerun_run_id} was not re-run", todo.new_body is not None) from err
    return Applied(wrote_body=todo.new_body is not None, reran=todo.rerun_run_id)
