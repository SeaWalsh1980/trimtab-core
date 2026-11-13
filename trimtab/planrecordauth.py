"""Whether the latest progress record's merge authorises it (ADR 0072).

plan-run used to check by hand, with a run of `gh api` reads, who merged the
plan's latest progress record and whether that merge counts. This module does
it as one command, from the server and the fetched default branch:

1. the latest record and the commit that added it come from `planrecord.latest`;
   no record yet is a pass with nothing to judge (a first run);
2. that commit belongs to exactly one PR merged into the default branch, and is
   that PR's merge commit;
3. the code gate: the admin's or the approver's last counted review is an
   approval on the PR's final head, and neither account's last review is a
   request for changes (`mergeauthority`'s rules, so the gate at merge and this
   check agree);
4. the merger: the admin's merge counts once the code gate passes. The
   approver's counts only when, as well, the head is
   `plan/<plan>/item-NN-partK` for this plan and this record, in this
   repository, against the default branch; every `plan-scope` job on the head
   is a real, passing one (`mergeauthority.plan_scope_job_result`); a ruleset
   that requires `plan-scope` bound to GitHub Actions was last changed before
   the merge; the PR's files, a rename's old name included, touch no admin-only
   path, read in full; and the base's server-side `plan-gate` job passed. Any
   other merger authorises nothing.

Every condition is judged and every failure named, not just the first. A read
that fails or returns a shape this does not expect raises, so the caller exits
2 and nothing is vouched for. The logins come from the caller, which takes them
from the instance, never from the PR.
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable
from dataclasses import dataclass

from trimtab import mergeauthority, planauth, planrecord, planscope
from trimtab.capture.sources import GhApi, Run, SourceError

PULLS_LIMIT = planauth.PULLS_LIMIT  # PRs one commit belongs to: one page; past it, an error
FILES_LIMIT = 3000  # what GitHub's PR files endpoint lists at most
RULESET_LIMIT = 50  # rulesets read for their times; far past any real repository
PLAN_GATE_JOB = "plan-gate"

PlanGateRead = Callable[[str, str], mergeauthority.PlanScopeResult]  # (repository, head) -> result


class RecordAuthError(Exception):
    """git or gh cannot answer, so no record is vouched for."""


@dataclass(frozen=True)
class Verdict:
    problems: tuple[str, ...]
    wait: bool = False  # the only problem is a plan-scope run still going: another read may answer differently
    record: str | None = None  # the latest record's path; None when there is none yet
    pr: int | None = None
    merged_by: str = ""
    approved_by: str = ""
    head: str = ""


@dataclass(frozen=True)
class _Pull:
    number: int
    merged_at: str
    merge_commit: str
    merged_by: str
    head_ref: str
    head: str
    head_repo: str
    base_ref: str
    changed_files: int


def check(repo: str, repository: str, plan: str, who: mergeauthority.Accounts, run: Run | None = None,
          plan_gate: PlanGateRead | None = None) -> Verdict:
    """Whether `plan`'s latest record in `repository`, whose checkout `repo` has fetched its default branch, was
    merged in a way that authorises it. The names are checked before any REST path or git call is built: a name
    that is wrong is a named problem, since no re-run fixes it."""
    problems = name_problems(repository, plan)
    if problems:
        return Verdict(problems)
    try:
        fetched = planrecord.fetched(repo, repository, run=run)
        problems = planrecord.plan_problems(repo, fetched.tip, plan)
        if problems:
            return Verdict(problems)
        record = planrecord.latest(repo, fetched.tip, plan)
        if record.problems:
            return Verdict(record.problems)
        if record.path is None:
            return Verdict(())
        # The checker's own code is admin-only where the repository runs it from its tree: as the merge gate has
        # it, judged by the base the PR merged into, the record commit's first parent on the default branch.
        base = planscope.commit(repo, f"{record.commit}^1")
        checker_in_tree = mergeauthority.CHECKER_MARK in planscope.present(repo, base, [mergeauthority.CHECKER_MARK])
    except planrecord.PlanRecordError as err:
        raise RecordAuthError(str(err)) from err
    except planscope.PlanScopeError as err:
        raise RecordAuthError(str(err)) from err
    return judge(repository, fetched.default, plan, record, who, checker_in_tree, run, plan_gate)


def judge(repository: str, default: str, plan: str, record: planrecord.Latest, who: mergeauthority.Accounts,
          checker_in_tree: bool, run: Run | None = None, plan_gate: PlanGateRead | None = None) -> Verdict:
    """Judge `record`'s merge from the server alone. `plan_gate` reads the base's server-side plan-gate job; None,
    the only value while the base ships no such job (trimtab-core ADR 0063 section 7), fails every approver merge."""
    _check_names(repository, plan)
    if record.path is None or record.commit is None or not planauth.SHA.fullmatch(record.commit):
        raise RecordAuthError("no record commit to judge")
    api = GhApi(run)
    found = _merged_pr(api, repository, default, record.commit)
    if isinstance(found, str):
        return Verdict((found,), record=record.path)
    pull = _read_pull(api, repository, found)
    problems = _pull_problems(pull, default, record.commit)
    if problems:
        return Verdict(problems, record=record.path, pr=pull.number, merged_by=pull.merged_by)

    reviews = _ma(mergeauthority.read_reviews, api, repository, pull.number)
    last = mergeauthority.last_reviews(reviews, (who.admin, who.approver))
    problems = [f"code gate: {p}" for p in mergeauthority._changes_requested(who, last)]
    approved = [login for login in (who.admin, who.approver)
                if login and mergeauthority._approves(last.get(login.casefold()), pull.head)]
    if not approved:
        names = f"{who.admin} or {who.approver}" if who.approver else who.admin
        problems.append(f"code gate: no approval from {names} on PR #{pull.number}'s final head {pull.head[:12]}")

    wait = False
    merger = pull.merged_by.casefold()
    if merger and merger == who.admin.casefold():
        pass
    elif merger and who.approver and merger == who.approver.casefold():
        route, wait = _approver_problems(api, repository, default, plan, record, pull, checker_in_tree, run, plan_gate)
        problems += route
    else:
        named = f"{who.admin} or the approver {who.approver}" if who.approver else \
            f"{who.admin} (the instance names no approver)"
        problems.append(f"PR #{pull.number} was merged by {pull.merged_by or 'a deleted account'}, not {named}")
    if problems:
        only_wait = wait and problems == [_WAITING]
        return Verdict(tuple(problems), wait=only_wait, record=record.path, pr=pull.number,
                       merged_by=pull.merged_by, head=pull.head)
    return Verdict((), record=record.path, pr=pull.number, merged_by=pull.merged_by, approved_by=approved[0],
                   head=pull.head)


_WAITING = (f"approver merge: {mergeauthority.PLAN_SCOPE_WORKFLOW}'s run or a {mergeauthority.CHECK_NAME} job on "
            "the final head has not completed; retry once it finishes")


def _approver_problems(api: GhApi, repository: str, default: str, plan: str, record: planrecord.Latest,
                       pull: _Pull, checker_in_tree: bool, run: Run | None,
                       plan_gate: PlanGateRead | None) -> tuple[list[str], bool]:
    """Every reason the approver's merge does not count, and whether the only one is a plan-scope run still going."""
    problems: list[str] = []
    item = posixpath.basename(record.path or "").removesuffix(".md")
    branch = planscope.plan_of(pull.head_ref)
    if branch.ref is not planscope.Ref.PLAN or branch.plan != plan or branch.item != item:
        problems.append(f"the head {pull.head_ref!r} is not plan/{plan}/{item}, the branch of the record it carries")
    pr = mergeauthority.Pr(repository, pull.head_repo, pull.head_ref, pull.head, pull.base_ref, default)
    # The fork and target tests only: a malformed branch is reported above, and paths below, read in full.
    reason = mergeauthority.admin_reason(pr, planscope.Branch(planscope.Ref.PLAN), [])
    if reason:
        problems.append(reason)
    problems += _file_problems(api, repository, pull, checker_in_tree)

    scope = _ma(mergeauthority.plan_scope_job_result, repository, pull.head, run)
    waiting = False
    if scope.wait:
        waiting = True
        problems.append(_WAITING)
    else:
        problems += scope.problems
    problems += _ruleset_problems(api, repository, default, pull)

    if plan_gate is None:
        problems.append(f"the pinned base ships no {PLAN_GATE_JOB} job (trimtab-core ADR 0063 section 7), so no "
                        "approver merge counts yet; only the admin's removal of plan:blocked accepts it")
    else:
        gate = plan_gate(repository, pull.head)
        if gate.wait:
            problems.append(f"the {PLAN_GATE_JOB} job on the final head has not completed")
        problems += gate.problems
    return [f"approver merge: {p}" if not p.startswith("approver merge:") else p for p in problems], waiting


def _file_problems(api: GhApi, repository: str, pull: _Pull, checker_in_tree: bool) -> list[str]:
    """Why the PR's files, read in full with each rename's old name, are not ones the approver may merge."""
    if pull.changed_files > FILES_LIMIT:
        return [f"the PR changes {pull.changed_files} files; GitHub lists at most {FILES_LIMIT}, so they cannot all "
                "be read"]
    what = "gh api pulls files"
    try:
        rows = api.all_pages(f"repos/{repository}/pulls/{pull.number}/files", {}, FILES_LIMIT, what)
    except SourceError as err:
        raise RecordAuthError(str(err)) from err
    names: list[str] = []
    for row in rows:
        name = row.get("filename") if isinstance(row, dict) else None
        old = row.get("previous_filename") if isinstance(row, dict) else None
        if not isinstance(name, str) or not name or (old is not None and (not isinstance(old, str) or not old)):
            raise RecordAuthError(f"{what} returned unexpected JSON")
        names += [name] + ([old] if old else [])
    if len(rows) != pull.changed_files:
        return [f"{what} listed {len(rows)} files but the PR changes {pull.changed_files}; refusing a partial list"]
    held = sorted({p for p in names if mergeauthority.admin_only(p, checker_in_tree)})
    if not held:
        return []
    shown = ", ".join(planscope.shown(p) for p in held[:mergeauthority.SHOWN_PATHS])
    more = f" and {len(held) - mergeauthority.SHOWN_PATHS} more" if len(held) > mergeauthority.SHOWN_PATHS else ""
    return [f"it changes admin-only paths: {shown}{more}"]


def _ruleset_problems(api: GhApi, repository: str, default: str, pull: _Pull) -> list[str]:
    """Why no ruleset shows that plan-scope, bound to GitHub Actions, was required when the PR merged. The API keeps
    no history of a ruleset's rules, so one changed since the merge, for any reason, cannot vouch for it."""
    what = "gh api rules branches"
    try:
        rows = api.all_pages(f"repos/{repository}/rules/branches/{default}", {}, mergeauthority.RULE_LIMIT, what)
    except SourceError as err:
        raise RecordAuthError(str(err)) from err
    ids: set[int] = set()
    for rule in rows:
        if not isinstance(rule, dict) or not isinstance(rule.get("type"), str):
            raise RecordAuthError(f"{what} returned unexpected JSON")
        if rule["type"] != "required_status_checks":
            continue
        params = rule.get("parameters")
        checks = params.get("required_status_checks") if isinstance(params, dict) else None
        if not isinstance(checks, list) or not all(isinstance(c, dict) for c in checks):
            raise RecordAuthError(f"{what} returned unexpected JSON")
        if any(c.get("context") == mergeauthority.CHECK_NAME
               and c.get("integration_id") == mergeauthority.ACTIONS_APP_ID for c in checks):
            ruleset = rule.get("ruleset_id")
            if not mergeauthority._app_id(ruleset):
                raise RecordAuthError(f"{what} returned unexpected JSON")
            ids.add(ruleset)
    if not ids:
        return []  # plan_scope_job_result names the missing requirement
    if len(ids) > RULESET_LIMIT:
        raise RecordAuthError(f"more than {RULESET_LIMIT} rulesets require {mergeauthority.CHECK_NAME}; "
                              "refusing a partial list")
    seen = []
    for ruleset in sorted(ids):
        row = _json(api, f"repos/{repository}/rulesets/{ruleset}", "gh api ruleset")
        updated = row.get("updated_at") if isinstance(row, dict) else None
        enforcement = row.get("enforcement") if isinstance(row, dict) else None
        if not (isinstance(updated, str) and mergeauthority.TIME.fullmatch(updated) and isinstance(enforcement, str)):
            raise RecordAuthError("gh api ruleset returned unexpected JSON")
        if enforcement == "active" and updated < pull.merged_at:
            return []
        seen.append(f"{ruleset} ({enforcement}, updated {updated})")
    return [f"no active ruleset requiring {mergeauthority.CHECK_NAME} bound to GitHub Actions was last changed "
            f"before the merge at {pull.merged_at}: {', '.join(seen)}; a ruleset changed since cannot show what it "
            "required then"]


def _merged_pr(api: GhApi, repository: str, default: str, sha: str) -> int | str:
    """The one PR merged into the default branch that the commit belongs to, or why there is not exactly one."""
    what = "gh api commit pulls"
    rows = _json(api, f"repos/{repository}/commits/{sha}/pulls", what, {"per_page": PULLS_LIMIT})
    if not isinstance(rows, list):
        raise RecordAuthError(f"{what} returned unexpected JSON")
    if len(rows) >= PULLS_LIMIT:
        raise RecordAuthError(f"{PULLS_LIMIT} or more PRs list {sha[:12]}; refusing to judge a partial list")
    merged = []
    for row in rows:
        base = row.get("base") if isinstance(row, dict) else None
        number = row.get("number") if isinstance(row, dict) else None
        if not isinstance(base, dict) or type(number) is not int or number < 1:
            raise RecordAuthError(f"{what} returned unexpected JSON")
        if row.get("merged_at") is not None and base.get("ref") == default:
            merged.append(number)
    if len(merged) != 1:
        listed = f" (#{', #'.join(map(str, sorted(merged)))})" if merged else ""
        return f"the record's commit {sha[:12]} belongs to {len(merged)} PRs merged into {default}{listed}, not one"
    return merged[0]


def _read_pull(api: GhApi, repository: str, number: int) -> _Pull:
    what = "gh api pull"
    row = _json(api, f"repos/{repository}/pulls/{number}", what)
    try:
        head, base = row["head"], row["base"]
        by = row["merged_by"]
        login = "" if by is None else by["login"]
        pull = _Pull(number, row["merged_at"] or "", row["merge_commit_sha"] or "", login, head["ref"], head["sha"],
                     (head["repo"] or {}).get("full_name") or "", base["ref"], row["changed_files"])
    except (KeyError, TypeError, AttributeError) as err:
        raise RecordAuthError(f"{what} returned unexpected JSON") from err
    if not (all(isinstance(v, str) for v in (pull.merged_at, pull.merge_commit, pull.merged_by, pull.head_ref,
                                              pull.head, pull.head_repo, pull.base_ref))
            and type(pull.changed_files) is int and pull.changed_files >= 0 and planauth.SHA.fullmatch(pull.head)
            and (not pull.merged_at or mergeauthority.TIME.fullmatch(pull.merged_at))):
        raise RecordAuthError(f"{what} returned unexpected JSON")
    return pull


def _pull_problems(pull: _Pull, default: str, sha: str) -> tuple[str, ...]:
    problems = []
    if not pull.merged_at:
        problems.append(f"PR #{pull.number} is not merged")
    if pull.base_ref != default:
        problems.append(f"PR #{pull.number} targets {pull.base_ref!r}, not {default}")
    if pull.merge_commit != sha:
        problems.append(f"PR #{pull.number}'s merge commit is {pull.merge_commit[:12] or 'none'}, not the record's "
                        f"commit {sha[:12]}")
    return tuple(problems)


def name_problems(repository: str, plan: str) -> tuple[str, ...]:
    bad = () if planauth.REPOSITORY.fullmatch(repository) and ".." not in repository else \
        (f"{repository!r} is not owner/name",)
    return bad + planrecord.name_problems(plan)


def _check_names(repository: str, plan: str) -> None:
    problems = name_problems(repository, plan)
    if problems:
        raise RecordAuthError("; ".join(problems))


def _json(api: GhApi, path: str, what: str, query: dict | None = None):
    try:
        return api.json("GET", path, what, query=query)
    except SourceError as err:
        raise RecordAuthError(str(err)) from err


def _ma(read, *args):
    try:
        return read(*args)
    except mergeauthority.MergeAuthorityError as err:
        raise RecordAuthError(str(err)) from err
