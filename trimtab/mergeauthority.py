"""The merge-authority check: whose approval a PR needs before it may merge (ADR 0043, building on ADR 0041 and 0042).

GitHub's code-owner rule works by path, not by the branch a PR comes from, so
it cannot let the approver account approve plan run PRs and nothing else.
This check, made a required check beside code-owner review narrowed to the
admin-only paths below, can:

- a PR from a branch outside `plan/` needs the admin's approval;
- a PR from `plan/` needs a `plan-scope` check run by GitHub Actions that
  passed on its head, and the approval of the admin, or of the approver when
  all of these hold: the branch is `plan/<plan>/item-NN-partK`, it is in this
  repository (not a fork), the PR targets the default branch, and it changes
  no admin-only path;
- an admin-only path is one guard-paths' layer 1 refuses (the harness and
  controls, the authorised plans folder among them), either root file that can
  pin the checker (`.claude/trimtab.json`, `instance.json`), or, in a
  repository that runs its checker from its own tree (one with `bin/trimtab`
  in the base commit), that code: `bin/`, `trimtab/`, `hooks/run_module.py`.

An approval counts only as the account's last review that is not `COMMENTED`
or `PENDING`, in state `APPROVED`, on the head commit. A last review in state
`DISMISSED` means the account has no review; an earlier one never stands in.
A last review in state `CHANGES_REQUESTED` from either account fails the PR.
This is the wrap-merged skill's code gate, so the gate at merge and the
skill's check after it agree.

The admin and approver logins come from the caller, never from the PR: the
job passes them from configuration the PR cannot change. Changed paths come
from git (`base...head`, renames off, as plan-scope reads them); reviews and
check runs from GitHub's REST API through `gh api`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from trimtab import planscope
from trimtab.capture.sources import PER_PAGE, GhApi, Run, SourceError

CHECK_NAME = "plan-scope"
ACTIONS_APP = "github-actions"
PIN_FILES = (".claude/trimtab.json", "instance.json")
CHECKER_MARK = "bin/trimtab"
CHECKER_DIRS = ("bin/", "trimtab/")
CHECKER_FILES = ("hooks/run_module.py",)
# Bounded (ENG-6.2): past these the answer is an error, never a partial list judged as whole.
REVIEW_LIMIT = 1000
CHECK_RUN_LIMIT = 1000
SHOWN_PATHS = 5
SHA = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
COUNTED = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")


class MergeAuthorityError(Exception):
    """The inputs, git or GitHub cannot answer, so no PR is vouched for."""


@dataclass(frozen=True)
class Review:
    id: int
    login: str
    state: str
    commit_id: str


@dataclass(frozen=True)
class CheckRun:
    id: int
    name: str
    app_slug: str
    status: str
    conclusion: str
    started_at: str


@dataclass(frozen=True)
class Pr:
    """What the platform's event says about the PR; the agent can forge none of it."""

    repository: str
    head_repo: str
    head_ref: str
    head: str
    base_ref: str
    default_branch: str


@dataclass(frozen=True)
class Accounts:
    admin: str
    approver: str  # "" when none is configured: then no approver review counts


@dataclass(frozen=True)
class Result:
    approved_by: str  # the login whose approval passed the PR; "" when it failed
    problems: tuple[str, ...]


def accounts(admin: str, approver: str | None) -> Accounts:
    """The configured logins, compared as GitHub compares them: without case."""
    admin, approver = admin.strip(), (approver or "").strip()
    if not admin:
        raise MergeAuthorityError("no admin login is configured")
    if approver.casefold() == admin.casefold():
        raise MergeAuthorityError("the approver and admin logins are the same; the configuration is wrong")
    return Accounts(admin, approver)


def admin_only(path: str, checker_in_tree: bool) -> bool:
    rel = path.lower()
    if planscope.refused(path) or rel in PIN_FILES:
        return True
    return checker_in_tree and (rel.startswith(CHECKER_DIRS) or rel in CHECKER_FILES)


def last_reviews(reviews: list[Review], logins: tuple[str, ...]) -> dict[str, Review]:
    """Each named account's last counted review, keyed by its casefolded login; a dismissal is kept, so it
    hides an earlier approval rather than letting it stand in."""
    wanted = {login.casefold() for login in logins if login}
    last: dict[str, Review] = {}
    for r in sorted(reviews, key=lambda r: r.id):
        key = r.login.casefold()
        if key in wanted and r.state in COUNTED:
            last[key] = r
    return last


def plan_scope_problem(runs: list[CheckRun]) -> str:
    """Why the head has no passing plan-scope check run from GitHub Actions; "" when it has one."""
    ours = [r for r in runs if r.name == CHECK_NAME and r.app_slug == ACTIONS_APP]
    if not ours:
        return f"no {CHECK_NAME} check run from GitHub Actions on the head commit"
    if any(r.status != "completed" for r in ours):
        return f"a {CHECK_NAME} check run on the head commit has not completed"
    # Every run, not the latest: a re-run reuses its old event's base, so a re-run of an earlier pass could
    # otherwise outweigh a later failure judged against the current base. A new commit starts over.
    failed = [r for r in ours if r.conclusion != "success"]
    if failed:
        latest = max(failed, key=lambda r: (r.started_at, r.id))
        return (f"a {CHECK_NAME} check run on the head commit concluded {latest.conclusion or 'nothing'}; "
                f"any run that did not pass voids the head, so a new commit is needed to start over")
    return ""


def decide(pr: Pr, who: Accounts, paths: list[str], checker_in_tree: bool, reviews: list[Review],
           runs: list[CheckRun]) -> Result:
    problems = []
    last = last_reviews(reviews, (who.admin, who.approver))
    for login in (who.admin, who.approver):
        r = last.get(login.casefold()) if login else None
        if r and r.state == "CHANGES_REQUESTED":
            problems.append(f"{login} requested changes; that voids every approval until they approve again")

    branch = planscope.plan_of(pr.head_ref)
    if branch.ref is planscope.Ref.NOT_PLAN:
        eligible, why = (who.admin,), "the head is not a plan branch"
    else:
        scope = plan_scope_problem(runs)
        if scope:
            problems.append(scope)
        why = admin_reason(pr, branch, [p for p in paths if admin_only(p, checker_in_tree)])
        eligible = (who.admin,) if why or not who.approver else (who.admin, who.approver)

    approved = [login for login in eligible if _approves(last.get(login.casefold()), pr.head)]
    if not approved:
        names = " or ".join(eligible)
        reason = f" ({why}, so only the admin's counts)" if why else ""
        stale = [login for login in eligible if _stale(last.get(login.casefold()), pr.head)]
        detail = f"; {' and '.join(stale)} approved an earlier commit" if stale else ""
        problems.append(f"no approval from {names} on the head commit {pr.head[:12]}{reason}{detail}")
    if problems:
        return Result("", tuple(problems))
    return Result(approved[0], ())


def admin_reason(pr: Pr, branch: planscope.Branch, held: list[str]) -> str:
    """Why a plan PR needs the admin rather than the approver; "" when the approver may approve it."""
    if branch.ref is planscope.Ref.MALFORMED:
        return "the head is under plan/ but is not plan/<plan>/item-NN-partK"
    if pr.head_repo.casefold() != pr.repository.casefold():
        return f"the head is in {pr.head_repo!r}, not this repository"
    if not planscope.targets_default(pr.base_ref, pr.default_branch):
        return f"the PR targets {pr.base_ref!r}, not the default branch"
    if held:
        shown = ", ".join(planscope.shown(p) for p in held[:SHOWN_PATHS])
        more = f" and {len(held) - SHOWN_PATHS} more" if len(held) > SHOWN_PATHS else ""
        return f"it changes admin-only paths: {shown}{more}"
    return ""


def _approves(r: Review | None, head: str) -> bool:
    return r is not None and r.state == "APPROVED" and r.commit_id == head


def _stale(r: Review | None, head: str) -> bool:
    return r is not None and r.state == "APPROVED" and r.commit_id != head


# ---- GitHub and git ------------------------------------------------------

def read_reviews(api: GhApi, repository: str, number: int) -> list[Review]:
    what = "gh api pulls reviews"
    try:
        rows = api.all_pages(f"repos/{repository}/pulls/{number}/reviews", {}, REVIEW_LIMIT, what)
    except SourceError as err:
        raise MergeAuthorityError(str(err)) from err
    found = []
    for r in rows:
        try:
            user = r["user"]
            review = Review(r["id"], user["login"] if user else "", r["state"], r.get("commit_id") or "")
        except (KeyError, TypeError) as err:
            raise MergeAuthorityError(f"{what} returned unexpected JSON") from err
        if not (isinstance(review.id, int) and all(isinstance(v, str) for v in
                                                    (review.login, review.state, review.commit_id))):
            raise MergeAuthorityError(f"{what} returned unexpected JSON")
        found.append(review)
    return found


def read_check_runs(api: GhApi, repository: str, head: str) -> list[CheckRun]:
    """Every plan-scope check run on the head commit, every page: `filter=all`, since the default keeps only
    the latest run per check suite, which could hide a later failure in another suite."""
    what = "gh api commits check-runs"
    rows: list = []
    page = 1
    while True:
        try:
            data = api.json("GET", f"repos/{repository}/commits/{head}/check-runs", what,
                            query={"check_name": CHECK_NAME, "filter": "all", "per_page": PER_PAGE, "page": page})
        except SourceError as err:
            raise MergeAuthorityError(str(err)) from err
        batch = data.get("check_runs") if isinstance(data, dict) else None
        if not isinstance(batch, list):
            raise MergeAuthorityError(f"{what} returned unexpected JSON")
        rows += batch
        if len(rows) > CHECK_RUN_LIMIT:
            raise MergeAuthorityError(f"{what} holds more than {CHECK_RUN_LIMIT} runs; refusing a partial list")
        if len(batch) < PER_PAGE:
            break
        page += 1
    found = []
    for r in rows:
        try:
            run = CheckRun(r["id"], r["name"], (r.get("app") or {}).get("slug") or "", r["status"],
                           r.get("conclusion") or "", r.get("started_at") or "")
        except (KeyError, TypeError, AttributeError) as err:
            raise MergeAuthorityError(f"{what} returned unexpected JSON") from err
        if not (isinstance(run.id, int) and all(isinstance(v, str) for v in
                                                 (run.name, run.app_slug, run.status, run.conclusion,
                                                  run.started_at))):
            raise MergeAuthorityError(f"{what} returned unexpected JSON")
        found.append(run)
    return found


def check(repo: str, pr: Pr, number: int, base: str, who: Accounts, run: Run | None = None) -> Result:
    """Judge a PR. Every input but `who` comes from the platform's event; base and head must be commits in
    repo (the job fetches the head, as plan-scope's does)."""
    for name, sha in (("base", base), ("head", pr.head)):
        if not SHA.fullmatch(sha):
            raise MergeAuthorityError(f"--{name} must be a 40-hex commit")
    if not REPOSITORY.fullmatch(pr.repository) or ".." in pr.repository:
        raise MergeAuthorityError("--repository must be OWNER/NAME")
    if number < 1:
        raise MergeAuthorityError("--pr must be a positive number")
    # Paths and plan-scope matter only on a plan head: a non-plan PR needs the admin whatever it changes, so
    # a diff git cannot read, or one past planscope.MAX_CHANGES, must not stop the admin's approval counting.
    is_plan = planscope.plan_of(pr.head_ref).ref is not planscope.Ref.NOT_PLAN
    paths: list[str] = []
    checker_in_tree = False
    if is_plan:
        try:
            paths = sorted({c.path for c in planscope.changes(repo, base, pr.head)})
            checker_in_tree = bool(planscope.present(repo, base, [CHECKER_MARK]))
        except planscope.PlanScopeError as err:
            raise MergeAuthorityError(str(err)) from err
    api = GhApi(run)
    reviews = read_reviews(api, pr.repository, number)
    runs = read_check_runs(api, pr.repository, pr.head) if is_plan else []
    return decide(pr, who, paths, checker_in_tree, reviews, runs)
