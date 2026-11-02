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

When the approver's approval is the one a PR rests on, the approver route must
also be open (ADR 0057): the agent opened the PR and alone pushed its branch,
in an unbroken chain from the branch's creation to the head; every required
check but this one has a latest run that passed from the App the ruleset binds
it to, and where that App is GitHub Actions, the run is a real passing job from
the workflow and event expected for it, since a forged run carries the same id;
no commit status sits in a required check's name; the ruleset's containment is
on; and the admin's last review is not a dismissal. All of it comes from the
server, never a commit's author. None of it touches the admin's own approval:
nothing is read for it, so a failed read can only stop the approver.

An approval counts only as the account's last review that is not `COMMENTED`
or `PENDING`, in state `APPROVED`, on the head commit. A last review in state
`DISMISSED` means the account has no review; an earlier one never stands in.
A last review in state `CHANGES_REQUESTED` from either account fails the PR.
This is the wrap-merged skill's code gate, so the gate at merge and the
skill's check after it agree.

The admin, approver and agent logins and the expected workflows come from the
caller, never from the PR: the job passes them from configuration the PR cannot change. Changed paths come
from git (`base...head`, renames off, as plan-scope reads them); reviews and
check runs from GitHub's REST API through `gh api`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from trimtab import planscope
from trimtab.capture.sources import PER_PAGE, GhApi, Run, SourceError

CHECK_NAME = "plan-scope"
OWN_CHECK = "merge-authority"
ACTIONS_APP = "github-actions"
ACTIONS_APP_ID = 15368
JOB_EVENT = "pull_request"
WORKFLOW_LOCK = ".github/**/*"
PIN_FILES = (".claude/trimtab.json", "instance.json")
CHECKER_MARK = "bin/trimtab"
CHECKER_DIRS = ("bin/", "trimtab/")
CHECKER_FILES = ("hooks/run_module.py",)
# Bounded (ENG-6.2): past these the answer is an error, never a partial list judged as whole.
REVIEW_LIMIT = 1000
CHECK_RUN_LIMIT = 1000
ACTIVITY_LIMIT = 100  # one page; a plan item with this many ref events is the admin's to judge
SHOWN_PATHS = 5
SHA = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
WORKFLOW = re.compile(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml")
STATUS_LIMIT = 1000
JOB_LIMIT = 50  # job reads per PR: one per Actions-bound required check, so far past any real ruleset
RULE_LIMIT = 1000
COUNTED = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")
NO_COMMIT = "0" * 40
CONTAINMENT = ("require_code_owner_review", "require_last_push_approval", "dismiss_stale_reviews_on_push",
               "require_extra_approval_for_unattributed_changes", "required_review_thread_resolution")


class MergeAuthorityError(Exception):
    """The inputs, git or GitHub cannot answer, so no PR is vouched for."""


RouteRead = Callable[[], "RouteFacts | None"]


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
    app_id: int | None = None  # None when the run has no App: it then matches no required check


@dataclass(frozen=True)
class Activity:
    """One row of the head branch's activity: who moved it, how, and between which commits."""

    id: int
    kind: str
    actor: str
    before: str
    after: str


@dataclass(frozen=True)
class RequiredCheck:
    context: str
    app_id: int | None  # None when the ruleset binds the check to no App


@dataclass(frozen=True)
class Containment:
    """What the default branch's rulesets hold, unioned across every rule in effect."""

    settings: frozenset[str]
    approvals: int
    strict: bool
    checks: tuple[RequiredCheck, ...]
    locked_paths: frozenset[str] = frozenset()


@dataclass(frozen=True)
class JobRecord:
    """The job behind a required check's latest run, and the workflow run it belongs to."""

    check_run_id: int
    name: str
    status: str
    conclusion: str
    runner_id: int | None
    runner_name: str
    steps: int
    head_sha: str
    run_head_sha: str
    path: str
    event: str


@dataclass(frozen=True)
class RouteFacts:
    """What the server reports, read when the approver's approval is the one the PR rests on."""

    author: str
    activities: tuple[Activity, ...]
    containment: Containment
    runs: tuple[CheckRun, ...]
    statuses: tuple[str, ...]
    jobs: tuple[JobRecord, ...] = ()
    workflows: Mapping[str, str] = field(default_factory=dict)  # each Actions-bound check's expected workflow


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
    agent: str = ""  # "" when none is configured: then the approver route is closed


@dataclass(frozen=True)
class Result:
    approved_by: str  # the login whose approval passed the PR; "" when it failed
    problems: tuple[str, ...]


def accounts(admin: str, approver: str | None, agent: str | None = None) -> Accounts:
    """The configured logins, compared as GitHub compares them: without case."""
    admin, approver = admin.strip(), (approver or "").strip()
    if not admin:
        raise MergeAuthorityError("no admin login is configured")
    agent = (agent or "").strip()
    if approver.casefold() == admin.casefold():
        raise MergeAuthorityError("the approver and admin logins are the same; the configuration is wrong")
    if agent and agent.casefold() in (admin.casefold(), approver.casefold()):
        raise MergeAuthorityError("the agent login is also the admin or approver login; the configuration is wrong")
    return Accounts(admin, approver, agent)


def job_workflows(pairs: list[str] | None) -> dict[str, str]:
    """The `CHECK=WORKFLOW_PATH` pairs naming which workflow must run each Actions-bound required check."""
    found: dict[str, str] = {}
    for pair in pairs or []:
        name, _, path = pair.partition("=")
        if not name or not WORKFLOW.fullmatch(path) or name in found:
            raise MergeAuthorityError("--job-workflow must be CHECK=.github/workflows/FILE, once per check")
        found[name] = path
    return found


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
        outcome = f"concluded {latest.conclusion}" if latest.conclusion else "completed with no conclusion"
        return (f"a {CHECK_NAME} check run on the head commit {outcome}; "
                f"any run that did not pass voids the head, so a new commit is needed to start over")
    return ""


def push_problems(head: str, agent: str, activities: tuple[Activity, ...]) -> list[str]:
    """Why the head branch's history is not the agent's alone, unbroken, from its creation to the head."""
    if not activities:
        return ["the head branch has no recorded activity"]
    rows = sorted(activities, key=lambda a: a.id)
    problems = [f"{name} pushed to the head branch; only {agent} may" for name in
                sorted({a.actor or "an unknown account" for a in rows if a.actor.casefold() != agent.casefold()})]
    first = rows[0]
    if first.kind != "branch_creation" or first.before != NO_COMMIT:
        # Older rows may have aged out, and an older push could be anyone's.
        problems.append("the head branch's activity does not reach back to its creation")
    problems += [f"a {kind} on the head branch" for kind in
                 sorted({a.kind for a in rows[1:] if a.kind != "push"})]
    if any(earlier.after != later.before for earlier, later in zip(rows, rows[1:])):
        problems.append("a gap in the head branch's chain of pushes")
    if rows[-1].after != head:
        problems.append("the head branch's newest activity is not the head commit")
    return problems


def containment_problems(held: Containment) -> list[str]:
    """Which of the containment the approver leans on is off, on the rulesets the server says are in effect."""
    problems = [f"the ruleset has {name} off" for name in CONTAINMENT if name not in held.settings]
    if held.approvals < 1:
        problems.append("the ruleset's required_approving_review_count is below 1")
    if not held.strict:
        problems.append(f"the ruleset does not hold {CHECK_NAME} to a strict policy")
    bound = {c.context for c in held.checks if c.app_id == ACTIONS_APP_ID}
    problems += [f"the ruleset does not require {name} bound to GitHub Actions" for name in (CHECK_NAME, OWN_CHECK)
                 if name not in bound]
    problems += [f"the ruleset binds {c.context} to no App" for c in held.checks if c.app_id is None]
    if WORKFLOW_LOCK not in held.locked_paths:
        problems.append(f"the ruleset does not lock {WORKFLOW_LOCK}, so a workflow could be placed on any branch")
    return problems


def required_run_problems(checks: tuple[RequiredCheck, ...], runs: tuple[CheckRun, ...]) -> list[str]:
    """Which required check has no latest run that passed from its bound App. This check is skipped: it is the
    run judging, so it cannot vouch for its own slot; the approver's own step must read it before acting. The latest by
    id, not by start: a carried-over job keeps its old start, and a draft's red before a ready run's green is
    normal on one head."""
    problems = []
    for check in checks:
        if check.context == OWN_CHECK or check.app_id is None:
            continue
        latest = _latest(check, runs)
        if latest is None:
            problems.append(f"required check {check.context} has no run from its bound App on the head commit")
        elif not _passed(latest):
            problems.append(f"required check {check.context}'s latest run has not passed")
    return problems


def job_problems(head: str, checks: tuple[RequiredCheck, ...], runs: tuple[CheckRun, ...], jobs: tuple[JobRecord, ...],
                 workflows: Mapping[str, str]) -> list[str]:
    """Which Actions-bound required check's passing run is not a real job. A forged run has the App's id and no
    runner or steps; a job from a forging workflow has a runner but, unless it sits at the expected path, another
    workflow and event; and a forged run can borrow a real run's identity. So neither test is enough alone, and
    together they still rest on the workflow lock and plan-scope's every-run rule (ADR 0057). A run that has not passed
    is `required_run_problems`' to report."""
    found = {job.check_run_id: job for job in jobs}
    problems = []
    for check, run in _jobs_to_read(checks, runs):
        expected = workflows.get(check.context)
        if not expected:
            problems.append(f"required check {check.context} has no expected workflow configured")
        elif not _real_job(found.get(run.id), check.context, head, expected):
            problems.append(f"required check {check.context}'s latest run is not a passing job from {expected}")
    return problems


def _latest(check: RequiredCheck, runs: tuple[CheckRun, ...]) -> CheckRun | None:
    ours = [r for r in runs if r.name == check.context and r.app_id == check.app_id]
    return max(ours, key=lambda r: r.id) if ours else None


def _passed(run: CheckRun) -> bool:
    return run.status == "completed" and run.conclusion == "success"


def _jobs_to_read(checks: tuple[RequiredCheck, ...],
                  runs: tuple[CheckRun, ...]) -> list[tuple[RequiredCheck, CheckRun]]:
    """Each Actions-bound required check but this one, with its latest run, where that run passed."""
    latest = ((c, _latest(c, runs)) for c in checks if c.app_id == ACTIONS_APP_ID and c.context != OWN_CHECK)
    return [(c, r) for c, r in latest if r is not None and _passed(r)]


def _real_job(job: JobRecord | None, name: str, head: str, workflow: str) -> bool:
    return (job is not None and job.name == name and job.status == "completed" and job.conclusion == "success"
            and job.runner_id is not None and bool(job.runner_name) and job.steps > 0
            and job.head_sha == head and job.run_head_sha == head and job.path == workflow and job.event == JOB_EVENT)


def route_problems(who: Accounts, pr: Pr, last: dict[str, Review], checker_in_tree: bool,
                   read: RouteRead) -> list[str]:
    """Why the approver's approval cannot carry this PR; empty when the approver route is open."""
    if not who.agent:
        return ["no agent login is configured, so the approver route is closed"]
    if checker_in_tree:
        # A second PR into a branch the agent wrote would run that branch's checker, so plan-scope's refusal of
        # that PR, which the route rests on, proves nothing here.
        return ["approver route closed: the repository runs its checker from its own tree"]
    facts = read()
    if facts is None:
        return ["the approver route's facts were not read"]
    problems = []
    if facts.author.casefold() != who.agent.casefold():
        problems.append(f"the PR was opened by {facts.author or 'an unknown account'}, not {who.agent}")
    problems += push_problems(pr.head, who.agent, facts.activities)
    problems += required_run_problems(facts.containment.checks, facts.runs)
    problems += job_problems(pr.head, facts.containment.checks, facts.runs, facts.jobs, facts.workflows)
    required = {c.context for c in facts.containment.checks}
    # The required name is what gets printed: a status's own context is anyone's text.
    problems += [f"a commit status is named for required check {name}"
                 for name in sorted(required & set(facts.statuses))]
    problems += containment_problems(facts.containment)
    admin = last.get(who.admin.casefold())
    if admin and admin.state == "DISMISSED":
        problems.append(f"{who.admin}'s last review was dismissed")
    return [f"approver route closed: {p}" for p in problems]


def decide(pr: Pr, who: Accounts, paths: list[str], checker_in_tree: bool, reviews: list[Review],
           runs: list[CheckRun], route: RouteRead = lambda: None) -> Result:
    """Judge the PR. `route` is called only when the approver's approval is the one the PR would pass on, so a
    failed read can only stop the approver."""
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
    elif approved[0] == who.approver:
        problems += route_problems(who, pr, last, checker_in_tree, route)
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


def read_check_runs(api: GhApi, repository: str, head: str, name: str | None = CHECK_NAME) -> list[CheckRun]:
    """Every check run on the head commit named `name` (None: under any name), every page: `filter=all`, since
    the default keeps only the latest run per check suite, which could hide a later failure in another suite."""
    what = "gh api commits check-runs"
    named = {"check_name": name} if name else {}
    rows: list = []
    page = 1
    while True:
        try:
            data = api.json("GET", f"repos/{repository}/commits/{head}/check-runs", what,
                            query={**named, "filter": "all", "per_page": PER_PAGE, "page": page})
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
                           r.get("conclusion") or "", r.get("started_at") or "", (r.get("app") or {}).get("id"))
        except (KeyError, TypeError, AttributeError) as err:
            raise MergeAuthorityError(f"{what} returned unexpected JSON") from err
        if not (isinstance(run.id, int) and all(isinstance(v, str) for v in
                                                 (run.name, run.app_slug, run.status, run.conclusion,
                                                  run.started_at))):
            raise MergeAuthorityError(f"{what} returned unexpected JSON")
        # Only the approver route reads the App's id, so a bad one must not stop the admin's approval.
        if name is None and not (run.app_id is None or _app_id(run.app_id)):
            raise MergeAuthorityError(f"{what} returned unexpected JSON")
        found.append(run)
    return found


def read_author(api: GhApi, repository: str, number: int) -> str:
    """The login that opened the PR; "" for a deleted account."""
    what = "gh api pulls"
    data = _json(api, f"repos/{repository}/pulls/{number}", what)
    if not isinstance(data, dict) or "user" not in data:
        raise MergeAuthorityError(f"{what} returned unexpected JSON")
    return _login(data["user"], what)


def read_activities(api: GhApi, repository: str, ref: str) -> tuple[Activity, ...]:
    """The head branch's activity. One full page is an error rather than a cut-off history: the endpoint pages by
    cursor, and a plan item with that many ref events is the admin's to judge."""
    what = "gh api activity"
    rows = _json(api, f"repos/{repository}/activity", what,
                 query={"ref": f"refs/heads/{ref}", "per_page": ACTIVITY_LIMIT})
    if not isinstance(rows, list):
        raise MergeAuthorityError(f"{what} returned unexpected JSON")
    if len(rows) >= ACTIVITY_LIMIT:
        raise MergeAuthorityError(f"{what} holds {ACTIVITY_LIMIT} or more rows; refusing a partial list")
    found = []
    for r in rows:
        try:
            activity = Activity(r["id"], r["activity_type"], _login(r["actor"], what), r["before"], r["after"])
            row_ref = r["ref"]
        except (KeyError, TypeError) as err:
            raise MergeAuthorityError(f"{what} returned unexpected JSON") from err
        if not (isinstance(activity.id, int) and all(isinstance(v, str) for v in
                                                      (activity.kind, activity.before, activity.after))):
            raise MergeAuthorityError(f"{what} returned unexpected JSON")
        if row_ref != f"refs/heads/{ref}":
            raise MergeAuthorityError(f"{what} returned a row for another ref")
        found.append(activity)
    return tuple(found)


def read_containment(api: GhApi, repository: str, branch: str) -> Containment:
    what = "gh api rules branches"
    try:
        rows = api.all_pages(f"repos/{repository}/rules/branches/{branch}", {}, RULE_LIMIT, what)
    except SourceError as err:
        raise MergeAuthorityError(str(err)) from err
    settings: set[str] = set()
    approvals = 0
    strict = False
    checks: list[RequiredCheck] = []
    locked: set[str] = set()
    try:
        for rule in rows:
            params = rule.get("parameters") or {}
            if rule["type"] == "pull_request":
                settings |= {name for name in CONTAINMENT if params.get(name) is True}
                approvals = max(approvals, params.get("required_approving_review_count") or 0)
            elif rule["type"] == "required_status_checks":
                held = [RequiredCheck(c["context"], c.get("integration_id")) for c in params["required_status_checks"]]
                checks += held
                # Strict is per rule, so only the rule holding plan-scope makes a plan PR take the new base.
                strict = strict or (params.get("strict_required_status_checks_policy") is True
                                    and any(c.context == CHECK_NAME for c in held))
            elif rule["type"] == "file_path_restriction":
                paths = params["restricted_file_paths"]
                if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
                    raise MergeAuthorityError(f"{what} returned unexpected JSON")
                locked |= set(paths)
    except (KeyError, TypeError, AttributeError) as err:
        raise MergeAuthorityError(f"{what} returned unexpected JSON") from err
    if not all(isinstance(c.context, str) and (c.app_id is None or _app_id(c.app_id)) for c in checks):
        raise MergeAuthorityError(f"{what} returned unexpected JSON")
    return Containment(frozenset(settings), approvals, strict, tuple(checks), frozenset(locked))


def read_status_contexts(api: GhApi, repository: str, head: str) -> tuple[str, ...]:
    what = "gh api commits statuses"
    try:
        rows = api.all_pages(f"repos/{repository}/commits/{head}/statuses", {}, STATUS_LIMIT, what)
        contexts = tuple(r["context"] for r in rows)
    except SourceError as err:
        raise MergeAuthorityError(str(err)) from err
    except (KeyError, TypeError) as err:
        raise MergeAuthorityError(f"{what} returned unexpected JSON") from err
    if not all(isinstance(c, str) for c in contexts):
        raise MergeAuthorityError(f"{what} returned unexpected JSON")
    return contexts


def read_jobs(api: GhApi, repository: str, checks: tuple[RequiredCheck, ...], runs: tuple[CheckRun, ...],
              workflows: Mapping[str, str]) -> tuple[JobRecord, ...]:
    """The job and workflow run behind each Actions-bound required check's passing latest run. Two checks in one
    workflow share a run, which is read once."""
    wanted = _jobs_to_read(checks, runs)
    if len(wanted) > JOB_LIMIT:
        raise MergeAuthorityError(f"more than {JOB_LIMIT} required checks' jobs to read; refusing a partial list")
    workflow_runs: dict[int, tuple[str, str, str]] = {}
    found = []
    for check, run in wanted:
        if check.context not in workflows:
            continue  # job_problems reports it, and there is nothing to compare the job with
        job = _json(api, f"repos/{repository}/actions/jobs/{run.id}", "gh api actions jobs")
        run_id, record = _job_fields(job)
        if run_id not in workflow_runs:
            workflow_runs[run_id] = _workflow_run_fields(
                _json(api, f"repos/{repository}/actions/runs/{run_id}", "gh api actions runs"))
        found.append(JobRecord(run.id, *record, *workflow_runs[run_id]))
    return tuple(found)


def _job_fields(job) -> tuple[int, tuple]:
    what = "gh api actions jobs"
    try:
        run_id, steps = job["run_id"], job["steps"]
        fields = (job["name"], job["status"], job.get("conclusion") or "", job["runner_id"],
                  job["runner_name"] or "", len(steps), job["head_sha"])
    except (KeyError, TypeError, AttributeError) as err:
        raise MergeAuthorityError(f"{what} returned unexpected JSON") from err
    name, status, conclusion, runner_id, runner_name, _, head_sha = fields
    if not (_app_id(run_id) and isinstance(steps, list) and (runner_id is None or _app_id(runner_id))
            and all(isinstance(v, str) for v in (name, status, conclusion, runner_name, head_sha))):
        raise MergeAuthorityError(f"{what} returned unexpected JSON")
    return run_id, fields


def _workflow_run_fields(run) -> tuple[str, str, str]:
    what = "gh api actions runs"
    try:
        fields = (run["head_sha"], run["path"], run["event"])
    except (KeyError, TypeError) as err:
        raise MergeAuthorityError(f"{what} returned unexpected JSON") from err
    if not all(isinstance(v, str) for v in fields):
        raise MergeAuthorityError(f"{what} returned unexpected JSON")
    return fields


def read_route_facts(api: GhApi, pr: Pr, number: int, workflows: list[str] | None = None) -> RouteFacts:
    expected = job_workflows(workflows)
    containment = read_containment(api, pr.repository, pr.default_branch)
    runs = tuple(read_check_runs(api, pr.repository, pr.head, name=None))
    return RouteFacts(author=read_author(api, pr.repository, number),
                      activities=read_activities(api, pr.repository, pr.head_ref),
                      containment=containment,
                      runs=runs,
                      statuses=read_status_contexts(api, pr.repository, pr.head),
                      jobs=read_jobs(api, pr.repository, containment.checks, runs, expected),
                      workflows=expected)


def _json(api: GhApi, path: str, what: str, query: dict | None = None):
    try:
        return api.json("GET", path, what, query=query)
    except SourceError as err:
        raise MergeAuthorityError(str(err)) from err


def _app_id(value) -> bool:
    """A real server id: a positive int, and not a bool, which Python counts as an int."""
    return type(value) is int and value > 0


def _login(user, what: str) -> str:
    """A user object's login; "" for the null GitHub leaves in place of a deleted account."""
    if user is None:
        return ""
    login = user.get("login") if isinstance(user, dict) else None
    if not isinstance(login, str):
        raise MergeAuthorityError(f"{what} returned unexpected JSON")
    return login


def check(repo: str, pr: Pr, number: int, base: str, who: Accounts, run: Run | None = None,
          workflows: list[str] | None = None) -> Result:
    """Judge a PR. Every input but `who` and `workflows` comes from the platform's event; base and head must be
    commits in repo (the job fetches the head, as plan-scope's does). `workflows` is read only for the approver
    route, so a bad value there can't stop the admin's approval."""
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
    return decide(pr, who, paths, checker_in_tree, reviews, runs, lambda: read_route_facts(api, pr, number, workflows))
