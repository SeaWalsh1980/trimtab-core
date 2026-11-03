"""The plan authorisation check: wrap-merged's checks 1-3, run as one command (ADR 0059).

A plan run cannot type these checks itself: guard-paths' layer 1 refuses any
shell command that names the authorised plans folder, reads included (ADR
0039), and a plan run has no override. So the session types
`trimtab check-plan-authorised --plan <plan>`, which names no path, and this
module reads the folder from git:

1. the plan (`<plan>.md`) and its declaration (`<plan>.writes`) are both on
   the default branch, as fetched;
2. for each, the commit that last added it in first-parent order belongs to
   exactly one PR merged into the default branch, which the admin merged,
   compared without case; both files name the same PR and merge commit;
3. neither has changed between that merge commit and the default branch.

The fetched default branch must be the one the server reports, so a stale
fetch cannot hide a later edit. The admin login comes from the caller, which
takes it from the instance once it matches the repository's variable
(`mergeauthority.plan_admin_problems`), never from the plan. Every read that
fails or returns the wrong shape raises: nothing is vouched for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from trimtab import planscope
from trimtab.capture.sources import GhApi, Run, SourceError

REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
PLAN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")  # as a plan/<plan>/ branch names it
SHA = re.compile(r"[0-9a-f]{40}")
PULLS_LIMIT = 100  # PRs one commit belongs to: one page; past it, an error rather than a partial list


class PlanAuthError(Exception):
    """git or gh cannot answer, so no plan is vouched for."""


@dataclass(frozen=True)
class Authorisation:
    problems: tuple[str, ...]  # each opens with the check it fails: "check 1:", "check 2:" or "check 3:"
    pr: int | None = None
    merge_commit: str | None = None
    writes: str | None = None  # the declaration's text on the default branch, on a pass


def check(repo: str, repository: str, plan: str, admin: str, run: Run | None = None) -> Authorisation:
    """Whether `plan` is authorised in `repository`, whose checkout `repo` has fetched its default branch."""
    if not REPOSITORY.fullmatch(repository) or ".." in repository:
        raise PlanAuthError(f"{repository!r} is not owner/name")
    if not PLAN.fullmatch(plan) or ".." in plan:
        raise PlanAuthError(f"{plan!r} is not a plan name")
    if not admin:
        raise PlanAuthError("no admin login to compare the merge with")
    api = GhApi(run)
    default = _default_branch(api, repository)
    tip = _fetched_tip(repo, api, repository, default)
    files = [f"{planscope.AUTHORISED}/{plan}.md", f"{planscope.AUTHORISED}/{plan}.writes"]

    missing = [f for f in files if f not in _git(planscope.present, repo, tip, files)]
    if missing:
        return Authorisation(tuple(f"check 1: {f} is not on {default}" for f in missing))

    merges = {}
    problems = []
    for f in files:
        found = _merged_by_admin(repo, api, repository, default, tip, f, admin)
        if isinstance(found, str):
            problems.append(f"check 2: {f}: {found}")
        else:
            merges[f] = found
    if problems:
        return Authorisation(tuple(problems))
    if len(set(merges.values())) != 1:
        return Authorisation(("check 2: the plan and its declaration were added by different PRs",))
    number, merge = next(iter(merges.values()))

    edited = _git(planscope.changed, repo, merge, tip, files)  # a merge commit git doesn't hold fails the read
    if edited:
        return Authorisation(tuple(f"check 3: {f} changed after PR #{number} merged it" for f in edited))
    writes = _git(planscope.read_files, repo, tip, files[1:])[files[1]]
    if writes is None:
        raise PlanAuthError(f"{files[1]} on {default} is not a regular file")
    return Authorisation((), number, merge, writes)


def _merged_by_admin(repo: str, api: GhApi, repository: str, default: str, tip: str, path: str,
                     admin: str) -> tuple[int, str] | str:
    """The (PR, merge commit) that added path, when the admin merged it; otherwise why not."""
    added = _git(planscope.added_by, repo, tip, path)
    if added is None:
        return "no commit adds it"
    rows = _json(api, f"repos/{repository}/commits/{added}/pulls", "gh api commit pulls",
                 {"per_page": PULLS_LIMIT})
    if not isinstance(rows, list) or len(rows) >= PULLS_LIMIT:
        raise PlanAuthError("gh api commit pulls returned unexpected JSON")
    merged = [_listed_number(row) for row in rows if _merged_into(row, default)]
    if len(merged) != 1:
        return f"{added[:12]} belongs to {len(merged)} PRs merged into {default}, not one"
    number = merged[0]
    row = _json(api, f"repos/{repository}/pulls/{number}", "gh api pull")
    if not isinstance(row, dict) or not isinstance(row.get("merge_commit_sha"), str) \
            or not SHA.fullmatch(row["merge_commit_sha"]):
        raise PlanAuthError("gh api pull returned unexpected JSON")
    by = row.get("merged_by")
    login = by.get("login") if isinstance(by, dict) else None
    if by is not None and not isinstance(login, str):
        raise PlanAuthError("gh api pull returned unexpected JSON")
    if not login or login.casefold() != admin.casefold():
        return f"PR #{number} was merged by {login or 'a deleted account'}, not the admin {admin}"
    return number, row["merge_commit_sha"]


def _merged_into(row, default: str) -> bool:
    if not isinstance(row, dict) or not isinstance(row.get("base"), dict):
        raise PlanAuthError("gh api commit pulls returned unexpected JSON")
    return row.get("merged_at") is not None and row["base"].get("ref") == default


def _listed_number(row) -> int:
    number = row.get("number")
    if type(number) is not int or number < 1:
        raise PlanAuthError("gh api commit pulls returned unexpected JSON")
    return number


def _default_branch(api: GhApi, repository: str) -> str:
    row = _json(api, f"repos/{repository}", "gh api repository")
    default = row.get("default_branch") if isinstance(row, dict) else None
    if not isinstance(default, str) or not default:
        raise PlanAuthError("gh api repository returned unexpected JSON")
    return default


def _fetched_tip(repo: str, api: GhApi, repository: str, default: str) -> str:
    """origin's default branch as fetched, which must be where the server says the branch is."""
    tip = _git(planscope.commit, repo, f"refs/remotes/origin/{default}")
    row = _json(api, f"repos/{repository}/branches/{default}", "gh api branch")
    sha = row.get("commit", {}).get("sha") if isinstance(row, dict) and isinstance(row.get("commit"), dict) else None
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise PlanAuthError("gh api branch returned unexpected JSON")
    if sha != tip:
        raise PlanAuthError(f"origin/{default} is {tip[:12]} but {repository}'s {default} is {sha[:12]}; "
                            "run `git fetch origin` and retry")
    return tip


def _json(api: GhApi, path: str, what: str, query: dict | None = None):
    try:
        return api.json("GET", path, what, query=query)
    except SourceError as err:
        raise PlanAuthError(str(err)) from err


def _git(read, *args):
    try:
        return read(*args)
    except planscope.PlanScopeError as err:
        raise PlanAuthError(str(err)) from err
