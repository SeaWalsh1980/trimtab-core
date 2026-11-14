"""What a plan has in flight, and the one thing a run may resume (ADR 0081).

plan-run used to have the model list the plan's open PRs and pushed branches, ask by hand which of them
a run may build on, and read label events to see whether a `plan:blocked` stop had been cleared. This
module does it as one command, from the server alone, in two list reads:

1. **every PR, any state**, oldest first (`pulls?state=all`): the open ones whose head is
   `plan/<plan>/item-*` are the PRs in flight, and the heads of all of them say which branches have
   already had a PR;
2. **the plan's branches** (`git/matching-refs/heads/plan/<plan>/`): one named `plan/<plan>/item-*`
   that no PR of this repository has ever had as its head is a branch in flight.

An open PR is resumable only if its head branch has exactly the shape `plan/<plan>/item-NN-partK`, its
head is in this repository (never a fork), its author is the agent or the admin, and any `plan:blocked`
stop on it was cleared by the admin (`planlabels.check_stops`). A branch with no PR is resumable only
if its shape is right and the plan's newest stop anywhere was cleared, or there was none. Exactly one
resumable thing is the answer; none is "none in flight"; more than one, or any refusal, is a refusal
that names every problem, never a guess about which to take.

Every read that fails, every list cut off at its bound and every row that is not the shape this expects
raises, so the caller exits 2 and nothing is resumed. The logins come from the caller, which takes them
from the instance, never from a PR. What this does not decide: whether the work holds only the plan
run's own in-scope pushes (plan-run/references/in-flight.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from trimtab import mergeauthority, planauth, planlabels
from trimtab.capture.sources import GhApi, Run, SourceError

PULLS_LIMIT = 5000  # PRs read to find the plan's; past it, an error rather than a partial list
REFS_LIMIT = 500  # branches under plan/<plan>/; past it, an error
SHAPE = re.compile(r"item-[0-9]{2}-part[1-9][0-9]*")  # what follows plan/<plan>/ in a part's branch
NONE_IN_FLIGHT = "none in flight"


class InflightError(Exception):
    """gh or git cannot answer, or answers in a shape this does not expect, so nothing is resumed."""


@dataclass(frozen=True)
class Resume:
    branch: str
    sha: str  # the branch's head commit
    pr: int | None  # None: a branch that has never had a PR

    def line(self) -> str:
        return f"resume pr {self.pr} {self.branch} {self.sha}" if self.pr else f"resume branch {self.branch} {self.sha}"


@dataclass(frozen=True)
class Verdict:
    problems: tuple[str, ...]
    resume: Resume | None = None  # with no problems: the one thing to resume, or None for none in flight


@dataclass(frozen=True)
class _Pull:
    number: int
    ref: str
    sha: str
    head_repo: str | None  # None: the head's repository is deleted
    author: str | None  # None: the account is deleted
    labels: frozenset[str]  # casefolded


def check(repo: str, repository: str, plan: str, who: mergeauthority.Accounts, run: Run | None = None) -> Verdict:
    """What plan `plan` has in flight in `repository`, whose checkout `repo` has fetched its default branch."""
    problems = planlabels.name_problems(repository, plan=plan)
    if problems:
        return Verdict(problems)
    if not who.admin:
        raise InflightError("no admin login to compare the PRs' authors and label removals with")
    api = GhApi(run)
    try:
        return _check(api, repo, repository, plan, who, run)
    except (planlabels.LabelCheckError, SourceError) as err:
        raise InflightError(str(err)) from err


def _check(api: GhApi, repo: str, repository: str, plan: str, who: mergeauthority.Accounts, run: Run | None
           ) -> Verdict:
    prefix = f"plan/{plan}/"
    # Branches first: a PR opened in the gap is then seen on the later PR list and judged as a PR, where the other
    # order would call its branch one that never had a PR. A branch pushed in the gap is merely missed, not resumed.
    branches = _branches(api, repository, plan)
    opened, headed = _pulls(api, repository, prefix + "item-")
    problems: list[str] = []
    allowed = {a.casefold() for a in (who.agent, who.admin) if a}

    sound: list[_Pull] = []
    for pull in opened:
        why = _pull_problems(pull, prefix, repository, allowed)
        problems += [f"PR #{pull.number}: {p}" for p in why]
        if not why:
            sound.append(pull)
    orphans = {name: sha for name, sha in branches.items() if name not in headed}
    shaped = {name: sha for name, sha in orphans.items() if SHAPE.fullmatch(name[len(prefix):])}
    problems += [f"branch {name} has no PR and is not {prefix}item-NN-partK" for name in orphans if name not in shaped]

    # Read even when nothing is in flight, so that "none in flight" is never the answer for a mistyped plan.
    stops = planlabels.check_stops(repo, repository, plan, who, {p.number: p.labels for p in sound}, bool(shaped), run)
    if stops.problems:
        return Verdict(tuple(problems) + stops.problems)

    resumable: list[Resume] = []
    for pull in sound:
        why = stops.prs[pull.number]
        problems += list(why)
        if not why:
            resumable.append(Resume(pull.ref, pull.sha, pull.number))
    for name, sha in sorted(shaped.items()):
        problems += [f"branch {name} has no PR: {p}" for p in stops.branch]
        if not stops.branch:
            resumable.append(Resume(name, sha, None))
    if len(resumable) > 1:
        names = ", ".join(f"PR #{r.pr}" if r.pr else f"branch {r.branch}" for r in resumable)
        problems.append(f"{len(resumable)} things are in flight ({names}); a run resumes one and cannot tell which")
    if problems:
        return Verdict(tuple(problems))
    return Verdict((), resumable[0] if resumable else None)


def _pull_problems(pull: _Pull, prefix: str, repository: str, allowed: set[str]) -> list[str]:
    problems = []
    if not SHAPE.fullmatch(pull.ref[len(prefix):]):
        problems.append(f"head branch {pull.ref} is not {prefix}item-NN-partK")
    if pull.head_repo is None or pull.head_repo.casefold() != repository.casefold():
        problems.append(f"head is in {pull.head_repo or 'a deleted repository'}, not {repository}: a fork head "
                        "is never resumed or built on")
    if pull.author is None:
        problems.append("its author is a deleted account, not the agent or admin")
    elif pull.author.casefold() not in allowed:
        problems.append(f"its author is {pull.author}, not the agent or admin")
    return problems


def _pulls(api: GhApi, repository: str, prefix: str) -> tuple[list[_Pull], set[str]]:
    """The open PRs whose head ref starts with `prefix`, and the head refs of every PR of this repository, in any
    state, that start with it. Rows come oldest first, so a PR opened during the read lands on a later page."""
    what = "gh api pulls (the bound is planinflight.PULLS_LIMIT; a repository past it needs it raised)"
    rows = api.all_pages(f"repos/{repository}/pulls", {"state": "all", "sort": "created", "direction": "asc"},
                         PULLS_LIMIT, what)
    opened: list[_Pull] = []
    headed: set[str] = set()
    seen: set[int] = set()
    for row in rows:
        head = row.get("head") if isinstance(row, dict) else None
        ref = head.get("ref") if isinstance(head, dict) else None
        number = row.get("number") if isinstance(row, dict) else None
        state = row.get("state") if isinstance(row, dict) else None
        if not isinstance(ref, str) or type(number) is not int or number < 1 or state not in ("open", "closed"):
            raise InflightError(f"{what} returned unexpected JSON")
        if number in seen:
            raise InflightError(f"{what} lists PR #{number} twice; a PR opened while it was read shifted the pages, "
                                "run it again")
        seen.add(number)
        if not ref.startswith(prefix):
            continue
        repo_row = head.get("repo", ...)  # null is a deleted repository; a missing key is an unexpected shape
        full = repo_row.get("full_name") if isinstance(repo_row, dict) else None
        if not (repo_row is None or isinstance(full, str) and full):
            raise InflightError(f"{what} returned unexpected JSON")
        if state == "closed":
            if full and full.casefold() == repository.casefold():
                headed.add(ref)
            continue
        user, labels, sha = row.get("user", ...), row.get("labels"), head.get("sha")
        login = user.get("login") if isinstance(user, dict) else None
        if not (isinstance(sha, str) and planauth.SHA.fullmatch(sha) and (user is None or isinstance(login, str)
                and login) and isinstance(labels, list)
                and all(isinstance(x, dict) and isinstance(x.get("name"), str) for x in labels)):
            raise InflightError(f"{what} returned unexpected JSON")
        if full and full.casefold() == repository.casefold():
            headed.add(ref)
        opened.append(_Pull(number, ref, sha, full, login, frozenset(x["name"].casefold() for x in labels)))
    return opened, headed


def _branches(api: GhApi, repository: str, plan: str) -> dict[str, str]:
    """Each branch `plan/<plan>/item-*` and its head commit, from one listing of the plan's branches."""
    what = "gh api matching refs"
    prefix = f"plan/{plan}/"
    rows = api.all_pages(f"repos/{repository}/git/matching-refs/heads/{prefix}", {}, REFS_LIMIT, what)
    found: dict[str, str] = {}
    for row in rows:
        ref = row.get("ref") if isinstance(row, dict) else None
        obj = row.get("object") if isinstance(row, dict) else None
        sha = obj.get("sha") if isinstance(obj, dict) else None
        if not (isinstance(ref, str) and ref.startswith(f"refs/heads/{prefix}") and isinstance(sha, str)
                and planauth.SHA.fullmatch(sha)):
            raise InflightError(f"{what} returned unexpected JSON")
        name = ref[len("refs/heads/"):]
        if name in found:
            raise InflightError(f"{what} lists {name} twice")
        if name.startswith(f"{prefix}item-"):
            found[name] = sha
    return found
