"""What guard-github needs to judge a `plan/*` write: the environment, the facts, and the branch names git resolves.

`PlanSupport` holds one command's view. It reads `TRIMTAB_PLAN_RUN` from the hook's own environment
(a session cannot set a variable for its hook, only the operator before launch, which is how the
run's flag works), and builds `GitHubFacts` on the first fact a row asks for, so a command that
writes no `plan/*` ref reads nothing and starts no `gh`. A build that fails is a `FactsError` at
the read, which the rows turn into a refusal (ADR 0063 §5).
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from trimtab import instance, planfacts, planrows, roots
from trimtab.capture.ghapi import GhApi
from trimtab.enforcement import tools
from trimtab.planrows import Change, Facts, FactsError, Push, PrState

RUN_FLAG = "TRIMTAB_PLAN_RUN"


def build_facts(cwd: Path, environ: Mapping[str, str] = os.environ) -> Facts:
    """The real facts for the checkout at `cwd`: its origin repository, the gate's gh, the instance's keys."""
    try:
        git = tools.Git(environ=environ)
        repo = planfacts.repository_of(git.config_get(cwd, "remote.origin.url"))
        api = GhApi(tools.gh_runner(tools.GhAuth(environ)))
        try:
            found = instance.load(roots.instance_root(environ=environ))
        except (roots.InstanceError, instance.InstanceFileError):
            found = None  # the rows that need a key then refuse
        return planfacts.GitHubFacts(repo, api, git, cwd, found)
    except (tools.ToolError, OSError) as err:
        raise FactsError(f"the gate's git or gh cannot start ({type(err).__name__})") from err


class LazyFacts(Facts):
    """`Facts` that build the real ones on first use."""

    def __init__(self, cwd: Path, build=build_facts):
        self._cwd, self._build, self._real = cwd, build, None

    def _facts(self) -> Facts:
        if self._real is None:
            self._real = self._build(self._cwd)
        return self._real

    def open_pr(self, branch: str) -> PrState | None: return self._facts().open_pr(branch)
    def branch_head(self, branch: str) -> str | None: return self._facts().branch_head(branch)
    def secret_scan(self, head: str) -> str: return self._facts().secret_scan(head)

    def changes(self, branch: str, source: str | None, head: str) -> Sequence[Change]:
        return self._facts().changes(branch, source, head)

    def pushes(self, branch: str) -> Sequence[Push]: return self._facts().pushes(branch)
    def failing_heads(self, branch: str) -> set[str]: return self._facts().failing_heads(branch)
    def facts_run_heads(self, branch: str) -> set[str]: return self._facts().facts_run_heads(branch)
    def conflicted(self, number: int) -> bool: return self._facts().conflicted(number)

    def register_answer(self, branch: str, before: str, after: str) -> bool:
        return self._facts().register_answer(branch, before, after)

    def local_plan_branches(self) -> Sequence[str]: return self._facts().local_plan_branches()
    def pr_head(self, number: int, repo: str | None) -> str | None: return self._facts().pr_head(number, repo)


class PlanSupport:
    """One command's plan-run view: the flag, the facts, and how a bare push or `HEAD` resolves."""

    def __init__(self, cwd: Path, environ: Mapping[str, str] = os.environ, facts: Facts | None = None,
                 git: tools.Git | None = None):
        self.cwd = cwd
        self.plan_run = environ.get(RUN_FLAG) == "1"
        self.facts: Facts = facts or LazyFacts(cwd)
        self._environ, self._git = environ, git

    def _run(self, cwd: Path, *args: str) -> str | None:
        try:
            git = self._git or tools.Git(environ=self._environ)
            return git(cwd, *args).strip() or None
        except (tools.ToolError, OSError):
            return None

    def push_branch(self, cwd: Path | None = None) -> str | None:
        """The remote branch a bare `git push` writes: `@{push}` without its `refs/remotes/<remote>/`."""
        ref = self._run(cwd or self.cwd, "rev-parse", "--symbolic-full-name", "@{push}")
        if ref is None or not ref.startswith("refs/remotes/"):
            return None
        parts = ref.split("/", 3)  # refs, remotes, <remote>, <branch>
        return parts[3] if len(parts) == 4 and parts[3] else None

    def head_branch(self, cwd: Path | None = None) -> str | None:
        """The checked-out branch, which a `HEAD` refspec writes under the same name."""
        return self._run(cwd or self.cwd, "symbolic-ref", "--short", "-q", "HEAD")


def judge_push(args: Sequence[tuple[str, bool]], support: PlanSupport, cwd: Path | None) -> str | None:
    """Judge `git push <args>` run in `cwd` (None: not known, so nothing resolves). Returns why it cannot
    be read, or None; raises planrows.Refusal."""
    found = planrows.read_push(args, lambda: support.push_branch(cwd) if cwd else None,
                               lambda: support.head_branch(cwd) if cwd else None)
    if found.unreadable:
        return found.unreadable
    if found.broad:
        planrows.judge_broad(found.broad, plan_run=support.plan_run, facts=support.facts)
    for write in found.writes:
        planrows.judge_write(write, plan_run=support.plan_run, facts=support.facts)
    return None
