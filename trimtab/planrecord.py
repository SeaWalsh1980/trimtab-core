"""The latest progress record of a plan, and the work a restart takes from it (ADR 0071).

plan-run used to have the model read `git log` and pick "the latest merged
record" by hand. This module does it from the default branch's first-parent
history, in one local git call, so the answer is the same every time:

- the newest merge that added a record holds the latest record (commit dates
  inside a branch do not matter: first-parent order is merge order);
- when one commit added several records (a fast-forward or rebase merge of a
  multi-commit PR), the highest item number, then the highest part, is latest;
- a record whose name is not `item-NN-partK.md` in the plan's own folder is a
  named problem, never skipped.

A restart's next work comes only from the latest record's file name and its
`## Status` line, never from the rest of its text.

This is a convenience that makes the skill reliable; it enforces nothing.
The history must be whole (a shallow clone or a grafts file makes the oldest
commit read as adding every record) and the fetch must be current; both are
errors, not verdicts. Every read that fails or returns the wrong shape raises.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from trimtab import planauth, planscope
from trimtab.capture.sources import GhApi, Run

NAME = re.compile(r"item-([0-9]{2,})-part([0-9]+)\.md")
# The one line a record's ## Status carries (plan-item), with the item and part it names.
COMPLETE = re.compile(r"item ([0-9]+) complete")
CONTINUES = re.compile(r"item ([0-9]+) part ([0-9]+) complete; item continues")
UNFINISHED = re.compile(r"item ([0-9]+) part ([0-9]+) stopped unfinished")
# An item of a plan's todo list, as `Item 3`, `### Item 3`, `- [ ] Item 3` or a numbered line `3.`.
# A numbered line counts only at column 0: an indented one is a sub-step, not an item.
PLAN_ITEM = re.compile(r"^(?:[ \t]*(?:#{1,6}[ \t]+|[-*][ \t]+(?:\[.\][ \t]+)?)?(?:\*\*)?item[ \t-]*([0-9]+)\b"
                       r"|([0-9]+)[.)][ \t])", re.IGNORECASE)
MAX_RECORDS = 5000  # record paths git names in one answer; past it, an error rather than a partial list (ENG-6.2)


class PlanRecordError(Exception):
    """git or gh cannot answer, so no record is named."""


@dataclass(frozen=True)
class Latest:
    path: str | None  # None: no record yet
    commit: str | None  # the commit that added it, in first-parent order
    item: int
    part: int
    problems: tuple[str, ...] = ()


def latest(repo: str, plan: str, default: str) -> Latest:
    """The latest record on origin/<default>, or item 1 part 1 when it holds none. Assumes the fetch is current
    and the history whole; `fetched` checks both."""
    folder = f"{planscope.PLANS_DIR}/progress/{plan}/"
    out = _git(planscope.log_added, repo, f"refs/remotes/origin/{default}", folder)
    # Newest merge first: a SHA line, then the paths that commit added, then a blank line.
    groups: list[tuple[str, list[str]]] = []
    named = 0
    for line in out.splitlines():
        if planscope.SHA.fullmatch(line):
            groups.append((line, []))
        elif line:
            if not groups:
                raise PlanRecordError("git log named a path before any commit")
            groups[-1][1].append(line)
            named += 1
            if named > MAX_RECORDS:
                raise PlanRecordError(f"more than {MAX_RECORDS} records added; refusing to judge a partial list")
    for sha, paths in groups:
        if not paths:
            continue  # a merge whose first-parent diff added nothing under the folder
        found: list[tuple[int, int, str]] = []
        problems = []
        for path in paths:
            m = NAME.fullmatch(path[len(folder):]) if path.startswith(folder) else None
            if m is None:
                problems.append(f"{path} (added by {sha[:12]}) is not {folder}item-NN-partK.md")
            else:
                found.append((int(m.group(1)), int(m.group(2)), path))
        if problems:
            return Latest(None, sha, 0, 0, tuple(problems))
        item, part, path = max(found)
        # Only additions are listed, so a record deleted or moved since is still "latest" there.
        if path not in _git(planscope.present, repo, f"refs/remotes/origin/{default}", [path]):
            return Latest(None, sha, 0, 0, (f"{path} was added by {sha[:12]} but is not on {default}",))
        return Latest(path, sha, item, part)
    return Latest(None, None, 1, 1)


def plan_problems(repo: str, default: str, plan: str) -> tuple[str, ...]:
    """Why `plan` is not a plan on the fetched default branch, so that "no record yet" means a plan not started
    and never a mistyped name. Authorisation itself is `check-plan-authorised`'s."""
    if not planauth.PLAN.fullmatch(plan) or ".." in plan:
        return (f"{plan!r} is not a plan name",)
    held = _git(planscope.present, repo, f"refs/remotes/origin/{default}", [f"{planscope.AUTHORISED}/{plan}.md"])
    if not held:
        return (f"{plan} is not a plan on {default}",)
    return ()


def next_work(repo: str, default: str, record: Latest, plan_text: str) -> tuple[str, tuple[str, ...]]:
    """What a restart does after `record`: (`item NN part K` | `plan complete`, problems). Taken only from the
    record's file name and its `## Status` line; the plan's text supplies the item list and nothing else."""
    if record.path is None:
        return "item 01 part 1", ()
    tip = f"refs/remotes/origin/{default}"
    text = _git(planscope.read_files, repo, tip, [record.path])[record.path]
    if text is None:
        return "", (f"{record.path} is not a regular file on {default}",)
    status = _status(text)
    if status is None:
        return "", (f"{record.path} has no ## Status line",)
    kind, why = _status_fields(status, record)
    if kind is None:
        return "", (f"{record.path}: {why}",)
    if kind == "continues":
        return f"item {record.item:02d} part {record.part + 1}", ()
    items = plan_items(plan_text)
    if record.item not in items:  # an item list that does not parse is a fault, never "plan complete"
        return "", (f"the plan's item list does not name item {record.item}; it could not be read as a todo list",)
    following = record.item + 1
    if following in items:
        return f"item {following:02d} part 1", ()
    return "plan complete", ()


def _status(text: str) -> str | None:
    """The first non-blank line under `## Status`, without surrounding backticks, or None."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "## Status":
            for following in lines[i + 1:]:
                if following.startswith("## "):
                    return None
                if following.strip():
                    return following.strip().strip("`").strip()
            return None
    return None


def _status_fields(status: str, record: Latest) -> tuple[str | None, str]:
    """(`complete` | `continues`, "") or (None, why the line is not one the record's own name allows)."""
    m = COMPLETE.fullmatch(status)
    if m:
        if int(m.group(1)) != record.item:
            return None, f"## Status names item {int(m.group(1))}, the file name item {record.item}"
        return "complete", ""
    for pattern in (CONTINUES, UNFINISHED):
        m = pattern.fullmatch(status)
        if m:
            if (int(m.group(1)), int(m.group(2))) != (record.item, record.part):
                return None, (f"## Status names item {int(m.group(1))} part {int(m.group(2))}, the file name "
                              f"item {record.item} part {record.part}")
            return "continues", ""
    return None, "## Status is none of the three forms plan-item writes"


def plan_items(plan_text: str) -> frozenset[int]:
    """The item numbers the plan's todo list names."""
    found = set()
    for line in plan_text.splitlines():
        m = PLAN_ITEM.match(line)
        if m:
            found.add(int(m.group(1) or m.group(2)))
    return frozenset(found)


def fetched(repo: str, repository: str, default: str | None = None, run: Run | None = None) -> str:
    """The default branch's name, once origin's copy is whole and is where the server says the branch is."""
    api = GhApi(run)
    try:
        name = planauth.default_branch(api, repository)
        if default and default != name:
            raise PlanRecordError(f"{default} is not {repository}'s default branch ({name})")
        planauth.fetched_tip(repo, api, repository, name)
    except planauth.PlanAuthError as err:
        raise PlanRecordError(str(err)) from err
    cut = _git(planscope.history_cut, repo)
    if cut:
        raise PlanRecordError(cut)
    return name


def _git(read, *args):
    try:
        return read(*args)
    except planscope.PlanScopeError as err:
        raise PlanRecordError(str(err)) from err
    except (UnicodeDecodeError, ValueError) as err:
        raise PlanRecordError(f"git's answer to {read.__name__} could not be parsed") from err
