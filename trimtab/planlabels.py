"""Who set or cleared a plan label, read from the server's issue events (ADR 0080).

plan-run used to have the model read `gh api .../issues/<n>/events` by hand and
decide who added `plan:continue`, and which PR a restart is about. This module
does it as one command, from the server alone, in two modes:

1. **the actor of `plan:continue`** on one PR: the label is on the PR now, its
   most recent `labeled` event is the label's last event, and its actor is the
   agent, approver or admin account the instance names;
2. **a restart**: across every PR whose head is `plan/<plan>/item-*`, open or
   closed, and the PR that added the plan, the most recent `labeled` event for
   `plan:blocked` marks the stopped PR; the restart is valid only if the admin's
   `unlabeled` event for it follows, and the label is absent now. Their events come from one
   read of the repository-wide issue events, newest first, back to the plan's merge, never one
   request per PR.

Every failure is named, not just the first. A read that fails, a list cut off
at its bound, an event that is not the shape this expects, or events that are
not in order raise, so the caller exits 2 and nothing is vouched for. Two
labelled events this cannot order, or a label whose events and presence
disagree, are named problems, never a guess. The logins come from the caller,
which takes them from the instance, never from the PR.
"""

from __future__ import annotations

from dataclasses import dataclass

from trimtab import mergeauthority, planauth, planrecord, planscope
from trimtab.capture.sources import GhApi, Run, SourceError

CONTINUE_LABEL = "plan:continue"
BLOCKED_LABEL = "plan:blocked"
ACTOR_LABELS = (CONTINUE_LABEL,)  # the labels whose adder mode 1 judges
EVENTS_LIMIT = 1000  # issue events one PR is read for; past it, an error rather than a partial list
PULLS_LIMIT = 5000  # PRs a restart lists to find the plan's item PRs; past it, an error
EVENTS_PAGE = 100  # rows per page of the repository-wide issue events (REST's largest)
# Pages of the repository-wide events a restart reads, newest first, looking for the plan's merge time: about 5,000
# events since the plan merged. This repository's whole history is 15 pages, so 50 is 3.3 times that: a busier
# repository is reached only while the plan is recent enough. A read stays at most 50 calls (twice, with the
# snapshot). Past it the command exits 2 (ADR 0080).
WINDOW_PAGES_LIMIT = 50
LABEL_EVENTS = ("labeled", "unlabeled")


class LabelCheckError(Exception):
    """git or gh cannot answer, or answers in a shape this does not expect, so nothing is vouched for."""


@dataclass(frozen=True)
class Verdict:
    problems: tuple[str, ...]
    pr: int | None = None  # mode 1: the PR checked; mode 2: the stopped PR
    actor: str = ""  # the login that set (mode 1) or cleared (mode 2) the label


@dataclass(frozen=True)
class _Event:
    id: int
    at: str
    action: str  # "labeled" or "unlabeled"
    label: str
    actor: str | None  # None: the account is deleted


def name_problems(repository: str, pr: int | None = None, plan: str | None = None, label: str | None = None
                  ) -> tuple[str, ...]:
    """Why a name is not one at all. Checked before any REST path is built: no re-run fixes a typo."""
    problems: list[str] = []
    if not planauth.REPOSITORY.fullmatch(repository) or ".." in repository:
        problems.append(f"{repository!r} is not owner/name")
    if pr is not None and (type(pr) is not int or pr < 1):
        problems.append(f"{pr!r} is not a PR number")
    if plan is not None:
        problems += planrecord.name_problems(plan)
    if label is not None and label not in ACTOR_LABELS:
        problems.append(f"{label!r} is not a label whose actor this checks ({', '.join(ACTOR_LABELS)})")
    return tuple(problems)


def check_actor(repository: str, pr: int, label: str, who: mergeauthority.Accounts, run: Run | None = None
                ) -> Verdict:
    """Whether `label` is on PR `pr` of `repository` now and its latest `labeled` event, which must also be the
    label's last event, was by the agent, approver or admin."""
    problems = name_problems(repository, pr=pr, label=label)
    if problems:
        return Verdict(problems)
    api = GhApi(run)
    present = _labels_now(api, repository, pr)
    events = _events(api, repository, pr)
    mine = [e for e in events if e.label.casefold() == label.casefold()]
    problems = []
    if label.casefold() not in present:
        problems.append(f"{label} is not on PR #{pr}")
    labeled = [e for e in mine if e.action == "labeled"]
    if not labeled:
        problems.append(f"PR #{pr} has no labeled event for {label}")
        return Verdict(tuple(problems), pr)
    last = labeled[-1]
    if mine[-1].action != "labeled" and label.casefold() in present:
        problems.append(f"{label} is on PR #{pr}, but its last event is an unlabeled one: the events and the "
                        "label disagree")
    allowed = {a.casefold() for a in (who.agent, who.approver, who.admin) if a}
    if last.actor is None:
        problems.append(f"{label} was added to PR #{pr} by a deleted account, not the agent, approver or admin")
    elif last.actor.casefold() not in allowed:
        problems.append(f"{label} was added to PR #{pr} by {last.actor}, not the agent, approver or admin")
    return Verdict(tuple(problems), pr, last.actor or "")


def check_restart(repo: str, repository: str, plan: str, who: mergeauthority.Accounts, run: Run | None = None
                  ) -> Verdict:
    """Whether the stopped PR of `plan` in `repository`, whose checkout `repo` has fetched its default branch, had
    `plan:blocked` removed by the admin after it was last added, and no longer carries it. On a pass `pr` is the
    stopped PR."""
    problems = name_problems(repository, plan=plan)
    if problems:
        return Verdict(problems)
    if not who.admin:
        raise LabelCheckError("no admin login to compare the removal with")
    api = GhApi(run)
    try:
        fetched = planrecord.fetched(repo, repository, run=run)
        problems = planrecord.plan_problems(repo, fetched.tip, plan)
        if problems:
            return Verdict(problems)
        added = planscope.added_by(repo, fetched.tip, f"{planscope.AUTHORISED}/{plan}.md")
    except (planrecord.PlanRecordError, planscope.PlanScopeError, UnicodeDecodeError, ValueError) as err:
        raise LabelCheckError(str(err)) from err
    plan_pr = _adding_pr(api, repository, fetched.default, added)
    if isinstance(plan_pr, str):
        return Verdict((plan_pr,))
    since = _merged_at(api, repository, plan_pr)
    # Read twice and require the same answer: a PR opened or a stop labelled while the first read ran would be
    # missed, and the newest stop would then be an older, cleared one.
    candidates, marked = _snapshot(api, repository, plan, plan_pr, since)
    if _snapshot(api, repository, plan, plan_pr, since) != (candidates, marked):
        raise LabelCheckError(f"the PRs or label events of plan {plan} changed while they were read; run it again")
    latest = [(es[_last(es, "labeled")], n) for n, es in marked.items() if any(e.action == "labeled" for e in es)]
    if not latest:
        return Verdict((f"nothing to restart: none of the PRs {_listed(candidates)} has a {BLOCKED_LABEL} event",))
    newest = max(e.at for e, _ in latest)
    tied = sorted(n for e, n in latest if e.at == newest)
    if len(tied) > 1:
        return Verdict((f"PRs {_listed(tied)} were labelled {BLOCKED_LABEL} in the same second, {newest}; which "
                        "stopped last cannot be read",))
    stopped = tied[0]
    stop = next(e for e, n in latest if n == stopped)
    after = marked[stopped][marked[stopped].index(stop) + 1:]
    present = _labels_now(api, repository, stopped)

    problems = []
    if BLOCKED_LABEL in present:
        problems.append(f"{BLOCKED_LABEL} is still on PR #{stopped}")
    if not after:
        problems.append(f"no removal of {BLOCKED_LABEL} follows its stop on PR #{stopped} at {stop.at}")
        return Verdict(tuple(problems), stopped)
    if len(after) > 1:
        problems.append(f"{len(after)} events follow the stop of PR #{stopped}; a label is removed once")
        return Verdict(tuple(problems), stopped)
    removal = after[0]
    if removal.actor is None:
        problems.append(f"{BLOCKED_LABEL} was removed from PR #{stopped} by a deleted account, not the admin")
    elif removal.actor.casefold() != who.admin.casefold():
        problems.append(f"{BLOCKED_LABEL} was removed from PR #{stopped} by {removal.actor}, not the admin")
    return Verdict(tuple(problems), stopped, removal.actor or "")


def _snapshot(api: GhApi, repository: str, plan: str, plan_pr: int, since: str
              ) -> tuple[list[int], dict[int, list[_Event]]]:
    """Every candidate PR, and each one's `plan:blocked` events since the plan merged, as read now."""
    candidates = sorted({plan_pr, *_item_prs(api, repository, plan)})
    return candidates, _window(api, repository, since, frozenset(candidates), plan_pr)


def _last(events: list[_Event], action: str) -> int:
    return max(i for i, e in enumerate(events) if e.action == action)


def _listed(numbers) -> str:
    return ", ".join(f"#{n}" for n in numbers)


def _labels_now(api: GhApi, repository: str, number: int) -> frozenset[str]:
    """The labels the PR carries now (casefolded). The number must be a PR, and the server's own."""
    row = _json(api, f"repos/{repository}/issues/{number}", "gh api issue")
    labels = row.get("labels") if isinstance(row, dict) else None
    if not (isinstance(row, dict) and row.get("number") == number and isinstance(row.get("pull_request"), dict)
            and isinstance(labels, list) and all(isinstance(x, dict) and isinstance(x.get("name"), str)
                                                 for x in labels)):
        raise LabelCheckError(f"gh api issue returned unexpected JSON, or #{number} is not a PR")
    return frozenset(x["name"].casefold() for x in labels)


def _events(api: GhApi, repository: str, number: int) -> list[_Event]:
    """The PR's label events, oldest first, from every page of its issue events. Event ids must rise and times
    must not fall across the whole list: a list that is not in order is not read."""
    what = "gh api issue events"
    try:
        rows = api.all_pages(f"repos/{repository}/issues/{number}/events", {}, EVENTS_LIMIT, what)
    except SourceError as err:
        raise LabelCheckError(str(err)) from err
    found: list[_Event] = []
    last_id, last_at = 0, ""
    for row in rows:
        ident = row.get("id") if isinstance(row, dict) else None
        at = row.get("created_at") if isinstance(row, dict) else None
        kind = row.get("event") if isinstance(row, dict) else None
        if not (type(ident) is int and ident > 0 and isinstance(kind, str)
                and isinstance(at, str) and mergeauthority.TIME.fullmatch(at)):
            raise LabelCheckError(f"{what} returned unexpected JSON")
        if ident <= last_id or at < last_at:
            raise LabelCheckError(f"{what} are not in the order this reads them in (event {ident})")
        last_id, last_at = ident, at
        if kind in LABEL_EVENTS:
            found.append(_label_event(row, ident, at, kind, what))
    return found


def _label_event(row: dict, ident: int, at: str, kind: str, what: str) -> _Event:
    """A `labeled` or `unlabeled` row as an event. The label's name and the actor key must be there; an actor of
    null is a deleted account, and a missing key is an unexpected shape."""
    label, actor = row.get("label"), row.get("actor", ...)
    name = label.get("name") if isinstance(label, dict) else None
    login = actor.get("login") if isinstance(actor, dict) else None
    if not isinstance(name, str) or not name or not (actor is None or isinstance(login, str) and login):
        raise LabelCheckError(f"{what} returned unexpected JSON")
    return _Event(ident, at, kind, name, login)


def _window(api: GhApi, repository: str, since: str, candidates: frozenset[int], plan_pr: int
            ) -> dict[int, list[_Event]]:
    """Each candidate PR's `plan:blocked` events, oldest first, taken from the repository-wide issue events (one
    list, newest first) read page by page until an event older than `since` (the plan's merge) is seen, or the list
    ends having shown the plan PR's own `merged` event. Events older than `since` are dropped, and so are other PRs' events. Whole-list rules, since this is one
    list: every row needs an id, a time and an issue number; ids must fall and times must not rise from row to
    row. Past the page cap without reaching `since` is an error, never a shorter window."""
    what = "gh api repository issue events"
    found: dict[int, list[_Event]] = {n: [] for n in candidates}
    last_id, last_at = None, None
    merged_seen = False
    for page in range(1, WINDOW_PAGES_LIMIT + 1):
        rows = _json(api, f"repos/{repository}/issues/events", what, {"per_page": EVENTS_PAGE, "page": page})
        if not isinstance(rows, list):
            raise LabelCheckError(f"{what} returned unexpected JSON")
        reached = False
        for row in rows:
            ident = row.get("id") if isinstance(row, dict) else None
            at = row.get("created_at") if isinstance(row, dict) else None
            kind = row.get("event") if isinstance(row, dict) else None
            issue = row.get("issue") if isinstance(row, dict) else None
            number = issue.get("number") if isinstance(issue, dict) else None
            if not (type(ident) is int and ident > 0 and isinstance(kind, str) and isinstance(at, str)
                    and mergeauthority.TIME.fullmatch(at) and type(number) is int and number > 0):
                raise LabelCheckError(f"{what} returned unexpected JSON")
            if last_id is not None and (ident >= last_id or at > last_at):
                raise LabelCheckError(f"{what} are not in the order this reads them in (event {ident}); a new "
                                      "event shifted the pages, run it again")
            last_id, last_at = ident, at
            if kind == "merged" and number == plan_pr and at == since:
                merged_seen = True
            if at < since:
                reached = True
            elif kind in LABEL_EVENTS and number in candidates:
                event = _label_event(row, ident, at, kind, what)
                if event.label.casefold() == BLOCKED_LABEL:
                    found[number].insert(0, event)  # newest first in, oldest first out
        if reached:
            return found
        if len(rows) < EVENTS_PAGE:
            # A short page is the end of the list only if the list is whole back to the merge: the plan PR's own
            # `merged` event, which the server stamps with exactly `merged_at`, must have been read. A short page
            # that is not the end, or a list missing its tail, would otherwise pass as a complete window.
            if merged_seen:
                return found
            raise LabelCheckError(f"{what} ended before the plan's merge event (PR #{plan_pr} at {since}); "
                                  "refusing to work from a partial list")
    raise LabelCheckError(f"{what} holds more than {WINDOW_PAGES_LIMIT} pages since the plan merged at {since}; "
                          "refusing to work from a partial list")


def _merged_at(api: GhApi, repository: str, number: int) -> str:
    """When the PR was merged, as the server reports it."""
    row = _json(api, f"repos/{repository}/pulls/{number}", "gh api pull")
    at = row.get("merged_at") if isinstance(row, dict) else None
    if not (isinstance(row, dict) and row.get("number") == number and isinstance(at, str)
            and mergeauthority.TIME.fullmatch(at)):
        raise LabelCheckError(f"gh api pull returned unexpected JSON, or PR #{number} has no merge time")
    return at


def _item_prs(api: GhApi, repository: str, plan: str) -> list[int]:
    """Every PR, open or closed, whose head is `plan/<plan>/item-*`. Oldest first, so a PR opened while this reads
    lands on a later page and is not skipped."""
    what = "gh api pulls"
    try:
        rows = api.all_pages(f"repos/{repository}/pulls", {"state": "all", "sort": "created", "direction": "asc"},
                             PULLS_LIMIT, what)
    except SourceError as err:
        raise LabelCheckError(str(err)) from err
    prefix = f"plan/{plan}/item-"
    found = []
    for row in rows:
        head = row.get("head") if isinstance(row, dict) else None
        ref = head.get("ref") if isinstance(head, dict) else None
        number = row.get("number") if isinstance(row, dict) else None
        if not isinstance(ref, str) or type(number) is not int or number < 1:
            raise LabelCheckError(f"{what} returned unexpected JSON")
        if ref.startswith(prefix):
            found.append(number)
    return found


def _adding_pr(api: GhApi, repository: str, default: str, sha: str) -> int | str:
    """The one PR merged into the default branch that the plan's adding commit belongs to, or why there is not."""
    what = "gh api commit pulls"
    rows = _json(api, f"repos/{repository}/commits/{sha}/pulls", what, {"per_page": planauth.PULLS_LIMIT})
    if not isinstance(rows, list):
        raise LabelCheckError(f"{what} returned unexpected JSON")
    if len(rows) >= planauth.PULLS_LIMIT:
        raise LabelCheckError(f"{planauth.PULLS_LIMIT} or more PRs list {sha[:12]}; refusing to judge a partial list")
    merged = []
    for row in rows:
        base = row.get("base") if isinstance(row, dict) else None
        number = row.get("number") if isinstance(row, dict) else None
        if not isinstance(base, dict) or type(number) is not int or number < 1:
            raise LabelCheckError(f"{what} returned unexpected JSON")
        if row.get("merged_at") is not None and base.get("ref") == default:
            merged.append(number)
    if len(merged) != 1:
        return f"the commit {sha[:12]} that added the plan belongs to {len(merged)} PRs merged into {default}, not one"
    return merged[0]


def _json(api: GhApi, path: str, what: str, query: dict | None = None):
    try:
        return api.json("GET", path, what, query=query)
    except SourceError as err:
        raise LabelCheckError(str(err)) from err
