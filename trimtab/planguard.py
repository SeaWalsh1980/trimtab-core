"""What guard-github needs to judge a `plan/*` write: the environment, the facts, and the names git resolves.

`PlanSupport` holds one command's view. It reads `TRIMTAB_PLAN_RUN` from the hook's own environment
(a session cannot set a variable for its hook, only the operator before launch, which is how the
run's flag works), and builds `GitHubFacts` on the first fact a row asks for, so a command that
writes no `plan/*` ref reads nothing and starts no `gh`. A build that fails is a `FactsError` at
the read, which the rows turn into a refusal (ADR 0063 §5).

A push is judged against the checkout it runs in (`-C`) and the repository it writes: the named
remote's effective push URL, or the literal URL. The configuration a push runs under (the
repository's, and the global and system files, read for steering keys only) is read first, because
it can change what a push writes (`remote.<r>.mirror`, `remote.<r>.push`, `push.default`) or where
it goes (`pushurl`, `url.<base>.insteadOf`).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
    def secret_scan(self, head: str, branch: str) -> str: return self._facts().secret_scan(head, branch)

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
    def pr_base(self, number: int, repo: str | None) -> str | None: return self._facts().pr_base(number, repo)
    def repository(self) -> str: return self._facts().repository()


@dataclass(frozen=True)
class Stability:
    """Whether what a push resolves still holds when the push runs, given the git commands before it in the
    same command. The guard reads HEAD, the upstream, the diff and the configuration when the hook runs, before
    any of them has."""

    name: bool = True  # the checked-out branch (switch, checkout, branch, symbolic-ref, clone, init)
    content: bool = True  # the commits HEAD holds (commit, merge, rebase, reset, cherry-pick, pull, ...)
    config: bool = True  # the push configuration (config, remote, branch, switch, checkout, clone, init)


STABLE = Stability()
_GIT = r"(?<![A-Za-z0-9_.-])git\s+(?:-\S+\s+)*(?:{})(?![A-Za-z0-9_-])"
_NAME_CHANGE = re.compile(_GIT.format("switch|checkout|branch|symbolic-ref|worktree|clone|init"), re.I)
_CONTENT_CHANGE = re.compile(_GIT.format("commit|merge|rebase|reset|cherry-pick|am|apply|stash|pull|restore|revert|"
                                         "update-ref"), re.I)
_CONFIG_CHANGE = re.compile(_GIT.format("config|remote|branch|switch|checkout|clone|init|worktree"), re.I)


def stability_of(command: str) -> Stability:
    return Stability(not _NAME_CHANGE.search(command), not _CONTENT_CHANGE.search(command),
                     not _CONFIG_CHANGE.search(command))


def parse_config(raw: str) -> dict[str, list[str]]:
    """`git config --list -z` as {lower-case key: [values]}: entries end at NUL, a key ends at a newline."""
    out: dict[str, list[str]] = {}
    for entry in raw.split("\0"):
        if not entry:
            continue
        key, _, value = entry.partition("\n")
        out.setdefault(key.lower(), []).append(value)
    return out


_INTEGER = re.compile(r"[+-]?[0-9]+[kmg]?", re.I)


def _true(values: Sequence[str]) -> bool:
    """git's boolean: a bare key, true/yes/on, or any non-zero integer (with a k, m or g suffix)."""
    for v in values:
        word = v.strip().lower()
        if word in ("", "true", "yes", "on") or (_INTEGER.fullmatch(word) and int(word.rstrip("kmg")) != 0):
            return True
    return False


class PlanSupport:
    """One command's plan-run view: the flag, the facts, and how a bare push or `HEAD` resolves."""

    def __init__(self, cwd: Path, environ: Mapping[str, str] = os.environ, facts: Facts | None = None,
                 git: tools.Git | None = None):
        self.cwd = cwd
        self.plan_run = environ.get(RUN_FLAG) == "1"
        self._injected = facts is not None
        self.facts: Facts = facts or LazyFacts(cwd)
        self._by_dir: dict[str, Facts] = {str(cwd): self.facts}
        self._environ, self._git = environ, git

    def facts_for(self, cwd: Path) -> Facts:
        """The facts for the checkout a push runs in; injected facts stand for every directory."""
        if self._injected:
            return self.facts
        return self._by_dir.setdefault(str(cwd), LazyFacts(cwd))

    def _git_run(self, cwd: Path, *args: str) -> str:
        return (self._git or tools.Git(environ=self._environ))(cwd, *args)

    def _run(self, cwd: Path, *args: str) -> str | None:
        try:
            return self._git_run(cwd, *args).strip() or None
        except (tools.ToolError, OSError):
            return None

    def push_ref(self, cwd: Path | None = None) -> tuple[str, str] | None:
        """(remote, branch) a bare push writes: `@{push}` without its `refs/remotes/` prefix."""
        ref = self._run(cwd or self.cwd, "rev-parse", "--symbolic-full-name", "@{push}")
        if ref is None or not ref.startswith("refs/remotes/"):
            return None
        parts = ref.split("/", 3)  # refs, remotes, <remote>, <branch>
        return (parts[2], parts[3]) if len(parts) == 4 and parts[2] and parts[3] else None

    def push_branch(self, cwd: Path | None = None) -> str | None:
        found = self.push_ref(cwd)
        return found[1] if found else None

    def head_sha(self, cwd: Path | None = None) -> str | None:
        """The checkout's `HEAD` commit, which an E11 round entry names (ADR 0063 §5)."""
        found = self._run(cwd or self.cwd, "rev-parse", "--verify", "--end-of-options", "HEAD^{commit}")
        return found if found and len(found) == 40 and all(c in "0123456789abcdef" for c in found) else None

    def head_branch(self, cwd: Path | None = None) -> str | None:
        """The checked-out branch, which a `HEAD` refspec writes under the same name."""
        return self._run(cwd or self.cwd, "symbolic-ref", "--short", "-q", "HEAD")

    def _is_repository(self, cwd: Path) -> bool:
        if self._git is not None:  # an injected git answers for itself
            try:
                self._git(cwd, "rev-parse", "--git-dir")
            except (tools.ToolError, OSError):
                return False
            return True
        return tools.Git(environ=self._environ).is_repository(cwd)

    def gh_default_configured(self, cwd: Path) -> bool:
        """`gh repo set-default` was used: gh then acts on a repository that may not be origin's."""
        return any(key.startswith("remote.") and key.endswith(".gh-resolved") for key in self.config(cwd))

    def pr_branch(self, cwd: Path) -> str | None:
        """The branch `gh` takes the current PR from: the checked-out branch's upstream if it has one, else it."""
        head = self.head_branch(cwd)
        if head is None:
            return None
        merge = self.config(cwd).get(f"branch.{head.lower()}.merge", [])
        if len(merge) == 1 and merge[0].startswith("refs/heads/"):
            return merge[0][len("refs/heads/"):]
        return head

    def _config_text(self, cwd: Path) -> str:
        """`git config --list -z` as a push would see it. An injected git answers for itself; the gate's git reads
        global and system configuration too, which it ignores for objects but a push does not (reads only)."""
        if self._git is not None:
            return self._git(cwd, "config", "--list", "-z")
        git = tools.Git(environ=self._environ)
        env = git.env
        for key in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"):
            env.pop(key, None)
        for key in ("HOME", "XDG_CONFIG_HOME"):
            if key in self._environ:
                env[key] = self._environ[key]
        try:
            done = subprocess.run(git.argv(cwd, "config", "--list", "-z"), capture_output=True, env=env,
                                  timeout=tools.GIT_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as err:
            raise tools.ToolError(f"git config failed ({type(err).__name__})") from err
        if done.returncode != 0:
            raise tools.ToolError(f"git config exited {done.returncode}")
        return done.stdout.decode("utf-8", "replace")

    def config(self, cwd: Path) -> dict[str, list[str]]:
        """The repository's effective configuration at `cwd`; empty where there is no repository to push from."""
        try:
            return parse_config(self._config_text(cwd))
        except (tools.ToolError, OSError) as err:
            if not self._is_repository(cwd):
                return {}  # not a repository: a push there fails by itself
            # A repository the gate's git refuses (shallow, alternates, partial) is not "no repository".
            raise FactsError("the repository's configuration cannot be read") from err


def _destination(remote: str | None, config: Mapping[str, list[str]], support: PlanSupport, cwd: Path) -> str:
    """The owner/name a push writes, or a FactsError saying why it cannot be told."""
    name = remote
    if name is None:
        resolved = support.push_ref(cwd)
        name = resolved[0] if resolved else None
    if name is None:
        raise FactsError("the remote of a push cannot be resolved")
    if any(k.startswith("url.") and k.endswith(("insteadof", "pushinsteadof")) for k in config):
        raise FactsError("URL rewrites in the configuration can send a push elsewhere")
    if "://" in name or name.startswith("git@"):
        return planfacts.repository_of(name)
    urls = config.get(f"remote.{name.lower()}.pushurl") or config.get(f"remote.{name.lower()}.url")
    if not urls or len(urls) != 1:
        raise FactsError("the remote has no single URL")
    return planfacts.repository_of(urls[0])


def _steering(found: planrows.PushRead, config: Mapping[str, list[str]], support: PlanSupport, cwd: Path,
              facts: Facts) -> None:
    """Configuration that changes what a push writes (E15, E16): a mirror remote, a push refspec, matching."""
    name = (found.remote or "").lower()
    if not name and (resolved := support.push_ref(cwd)):
        name = resolved[0].lower()
    if f"remote.{name}.mirror" in config and _true(config[f"remote.{name}.mirror"]):
        planrows.check_e15(planrows.Write(planrows.PLAN_PREFIX, force=True))  # a mirror forces and prunes
    if found.bare or found.colonless:  # git maps these through remote.<r>.push
        if config.get(f"remote.{name}.push"):
            raise FactsError("this push takes its refspecs from configuration, which this guard does not read")
    if (found.bare or found.colonless) and any(
            v.lower() in ("upstream", "tracking") for v in config.get("push.default", [])) and any(
            value.casefold().startswith("refs/heads/plan/") for key, values in config.items()
            if key.startswith("branch.") and key.endswith(".merge") for value in values):
        raise FactsError("push.default=upstream would map this push onto a plan/* upstream branch")
    if found.bare:
        if "matching" in [v.lower() for v in config.get("push.default", [])] and facts.local_plan_branches():
            raise FactsError("push.default=matching would push every local plan/* branch")


def judge_push(args: Sequence[tuple[str, bool]], support: PlanSupport, cwd: Path | None,
               stable: Stability = STABLE, record=None) -> str | None:
    """Judge `git push <args>` run in `cwd` (None: not known, so nothing resolves). `stable` says what an earlier
    part of the same command changes about what the push resolves. Returns why it cannot be read, or None;
    raises planrows.Refusal."""
    found = planrows.read_push(args, lambda: support.push_branch(cwd) if cwd else None,
                               lambda: support.head_branch(cwd) if cwd else None)
    if found.unreadable:
        return found.unreadable
    plan_involved = bool(found.broad) or any(planrows.plan_branch(w.branch) for w in found.writes)
    if cwd is None:
        return "a push touching plan/* runs in a directory the guard cannot tell" if plan_involved else None
    if stable != STABLE:  # HEAD, @{push}, the diff and the configuration are read before the earlier part has run
        risky = ((plan_involved and not (stable.name and stable.content))
                 or ((found.bare or found.head_ref) and not stable.name)
                 or ((found.bare or found.colonless) and not stable.config))
        if risky:
            return "a push judged on state an earlier part of the command changes"
    facts = support.facts_for(cwd)
    try:
        config = support.config(cwd)
        _steering(found, config, support, cwd, facts)
        if plan_involved:  # only a plan write needs the repository it goes to
            ours = facts.repository()
            if _destination(found.remote, config, support, cwd).casefold() != ours.casefold():
                return f"a push to a repository other than {ours}, which this guard reads facts for"
    except FactsError as err:
        raise planrows.Refusal(f"a push cannot be judged ({err}); refusing (ADR 0063 §5)") from err
    if found.broad:
        planrows.judge_broad(found.broad, plan_run=support.plan_run, facts=facts, force=found.force,
                             delete=found.delete)
        if record is not None and _plan_branches(facts):
            # A broad write names refs this reading cannot list one by one, so the ledger could not record
            # which it wrote, and E13 would read the branch as holding work no run sent (ADR 0063 §1).
            raise planrows.Refusal("a broad write (--all, --mirror, --prune) that touches a plan/* branch "
                                   "cannot be recorded in the ledger; name the branch (ADR 0063 §1)")
    for write in found.writes:
        planrows.judge_write(write, plan_run=support.plan_run, facts=facts)
        if record is not None:
            record(support, write.branch, write.source)
    return None


def _plan_branches(facts: Facts) -> bool:
    try:
        return bool(facts.local_plan_branches())
    except FactsError as err:
        raise planrows.Refusal(f"the local plan/* branches are unreadable ({err}); refusing") from err
