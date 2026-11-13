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
# The plan's item list: one `## Items` section of top-level `N. ` lines, numbered 1..n (ADR 0071).
# Nothing outside the section is read, and a section holding anything else is a named problem, never a guess.
ITEMS_HEADING = re.compile(r"##[ \t]+Items[ \t]*", re.IGNORECASE)
STATUS_HEADING = re.compile(r"##[ \t]+Status[ \t]*", re.IGNORECASE)
SECTION_HEADING = re.compile(r"##[ \t]+\S")
ITEM_LINE = re.compile(r"([0-9]+)\.[ \t]+\S.*")
# An indented numbered line: ambiguous, since markdown reads one indented less than four spaces as an item of
# the same list, so a sub-step must be indented four spaces or a tab and anything shallower is refused.
SUB_ITEM = re.compile(r"([ \t]*)([0-9]+)[.)]([ \t]|$)")
SUB_INDENT = 4
INDENTED_HEADING = re.compile(r"[ \t]+#{1,6}[ \t]")
MAX_RECORDS = 5000  # record paths git names in one answer; past it, an error rather than a partial list (ENG-6.2)


class PlanRecordError(Exception):
    """git or gh cannot answer, so no record is named."""


@dataclass(frozen=True)
class Fetched:
    default: str  # the default branch's name, for messages
    tip: str  # the commit origin holds for it, which every read names


@dataclass(frozen=True)
class Latest:
    path: str | None  # None: no record yet
    commit: str | None  # the commit that added it, in first-parent order
    item: int
    part: int
    problems: tuple[str, ...] = ()


def latest(repo: str, tip: str, plan: str) -> Latest:
    """The latest record in `tip`'s first-parent history, or item 1 part 1 when it holds none. `tip` is the
    commit `fetched` resolved and checked, never a ref: a fetch landing mid-read must not change the answer."""
    folder = f"{planscope.PLANS_DIR}/progress/{plan}/"
    out = _git(planscope.log_added, repo, tip, folder)
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
        if path not in _git(planscope.present, repo, tip, [path]):
            return Latest(None, sha, 0, 0, (f"{path} was added by {sha[:12]} but is not on {tip[:12]}",))
        return Latest(path, sha, item, part)
    return Latest(None, None, 1, 1)


def name_problems(plan: str) -> tuple[str, ...]:
    """Why `plan` is not a plan name at all. Checked before any read, since no fetch can fix a typo."""
    if not planauth.PLAN.fullmatch(plan) or ".." in plan:
        return (f"{plan!r} is not a plan name",)
    return ()


def plan_problems(repo: str, tip: str, plan: str) -> tuple[str, ...]:
    """Why `plan` is not a plan in the checked commit, so that "no record yet" means a plan not started and
    never a mistyped name. Authorisation itself is `check-plan-authorised`'s."""
    problems = name_problems(plan)
    if problems:
        return problems
    files = [f"{planscope.AUTHORISED}/{plan}.md", f"{planscope.AUTHORISED}/{plan}.writes"]
    held = _git(planscope.present, repo, tip, files)
    return tuple(f"{plan} is not a plan on {tip[:12]}: {f.rsplit('.', 1)[1]} file missing" for f in files
                 if f not in held)


def next_work(repo: str, tip: str, record: Latest, plan_text: str) -> tuple[str, tuple[str, ...]]:
    """What a restart does after `record`: (`item NN part K` | `plan complete`, problems). Taken only from the
    record's file name and its `## Status` line; the plan's text supplies the item list and nothing else.
    `tip` is the commit `fetched` checked, as in `latest`."""
    if record.path is None:
        return "item 01 part 1", ()
    text = _git(planscope.read_files, repo, tip, [record.path])[record.path]
    if text is None:
        return "", (f"{record.path} is not a regular file on {tip[:12]}",)
    status, why = _status(text)
    if status is None:
        return "", (f"{record.path} {why}",)
    kind, why = _status_fields(status, record)
    if kind is None:
        return "", (f"{record.path}: {why}",)
    # The plan's items are read on both paths: a part of an item the plan does not name is no more work than a
    # next item it does not name, and an unreadable section must not pass as either.
    items, problems = plan_items(plan_text)
    if problems:
        return "", problems
    if record.item not in items:
        return "", (f"the plan's ## Items section names items 1 to {items[-1]}, not item {record.item}",)
    if kind == "continues":
        return f"item {record.item:02d} part {record.part + 1}", ()
    if record.item == items[-1]:  # the numbers are 1..n, so the last item is the only end of the plan
        return "plan complete", ()
    return f"item {record.item + 1:02d} part 1", ()


def _status(text: str) -> tuple[str | None, str]:
    """The one line under the record's `## Status`, without surrounding backticks: (line, "") or (None, why).

    Read exactly as strictly as the plan's `## Items` section, and for the same reason: the record's prose is
    the least trusted input here, and a status the rendered record does not show must not decide the next
    work. So the heading counts only at the start of a line, a second one is refused rather than ignored, and
    the text is split on newlines alone."""
    lines = _lines(text)
    if any("\r" in line for line in lines):
        return None, "holds a carriage return inside a line; markdown would read it as a line ending"
    heads = [i for i, line in enumerate(lines) if STATUS_HEADING.fullmatch(line)]
    if not heads:
        return None, "has no ## Status heading"
    if len(heads) > 1:  # a quoted or fenced example counts: the reader cannot tell which the author meant
        return None, f"has {len(heads)} ## Status headings, not one"
    for following in lines[heads[0] + 1:]:
        if SECTION_HEADING.match(following):
            break
        if following.strip():
            return following.strip().strip("`").strip(), ""
    return None, "has no line under its ## Status heading"


def _lines(text: str) -> list[str]:
    """Lines split on newlines alone, with a trailing CR removed. str.splitlines would also break on \x0b,
    \x0c and \x85, which markdown does not, so a line only one of those begins could read as content."""
    return [line.removesuffix("\r") for line in text.split("\n")]


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


def plan_items(plan_text: str) -> tuple[tuple[int, ...], tuple[str, ...]]:
    """The plan's items as (1, 2, ... n), or why its `## Items` section cannot be read.

    The plan holds exactly one `## Items` heading. Its section runs to the next `## ` heading or the end of the
    file, and every non-blank line in it is either a top-level item `N. <text>`, numbered from 1 with no gap,
    no repeat and no reordering, or a line indented under one (an item's own wrapped text or sub-steps). Any
    other line, and a missing or repeated heading, is a named problem: the plan is not read as a todo list at
    all, so nothing outside the section can add or hide an item.

    A numbered line indented less than four spaces is refused rather than read as a sub-step, because markdown
    reads it as an item of the same list: it is the one shape that could hide a later item. So is an indented
    heading, which markdown may read as ending the section."""
    lines = _lines(plan_text)
    # A bare CR is a line ending to markdown but not to the split above, so a line holding one could carry an
    # item this reads as text. Refuse rather than decide which of the two readings is the author's.
    if any("\r" in line for line in lines):
        return (), ("the plan holds a carriage return inside a line; markdown would read it as a line ending",)
    heads = [i for i, line in enumerate(lines) if ITEMS_HEADING.fullmatch(line)]
    if not heads:
        return (), ("the plan has no ## Items section",)
    if len(heads) > 1:
        return (), (f"the plan has {len(heads)} ## Items headings, not one",)
    found: list[int] = []
    for line in lines[heads[0] + 1:]:
        if SECTION_HEADING.match(line):
            break
        if not line.strip():
            continue
        if line[:1] in (" ", "\t"):
            if not found:  # indented text before any item: the section's shape is not one this reads
                return (), ("the plan's ## Items section opens with an indented line, not item 1",)
            sub = SUB_ITEM.match(line)
            if sub and len(sub.group(1).expandtabs(SUB_INDENT)) < SUB_INDENT:
                return (), (f"the plan's ## Items section indents a numbered line, {sub.group(2)}., by less than "
                            f"{SUB_INDENT} spaces: indent a sub-step by {SUB_INDENT} or write it as an item at "
                            "the start of the line",)
            if INDENTED_HEADING.match(line):  # markdown may read it as ending the section, this does not
                return (), (f"the plan's ## Items section holds an indented heading: {line.strip()[:60]!r}",)
            continue  # an item's own wrapped text or sub-step
        m = ITEM_LINE.fullmatch(line)
        if m is None:
            return (), (f"the plan's ## Items section holds a line that is not an item: {line.strip()[:60]!r}",)
        if int(m.group(1)) != len(found) + 1:
            return (), (f"the plan's ## Items section names item {int(m.group(1))} where item {len(found) + 1} "
                        "should be; the items run 1 to n in order",)
        found.append(int(m.group(1)))
    if not found:
        return (), ("the plan's ## Items section names no item",)
    return tuple(found), ()


def fetched(repo: str, repository: str, default: str | None = None, run: Run | None = None) -> Fetched:
    """The default branch's name and the commit origin holds for it, once that copy is whole and is where the
    server says the branch is. Every later read names the commit, never the ref, so a fetch landing in between
    cannot slip a different history under the answer (as `planauth.check` pins its own tip)."""
    api = GhApi(run)
    try:
        name = planauth.default_branch(api, repository)
        if default and default != name:
            raise PlanRecordError(f"{default} is not {repository}'s default branch ({name})")
        tip = planauth.fetched_tip(repo, api, repository, name)
    except planauth.PlanAuthError as err:
        raise PlanRecordError(str(err)) from err
    cut = _git(planscope.history_cut, repo)
    if cut:
        raise PlanRecordError(cut)
    return Fetched(name, tip)


def _git(read, *args):
    try:
        return read(*args)
    except planscope.PlanScopeError as err:
        raise PlanRecordError(str(err)) from err
    except (UnicodeDecodeError, ValueError) as err:
        raise PlanRecordError(f"git's answer to {read.__name__} could not be parsed") from err
