"""The plan-run acts the guards recognise, and the ledger they write (ADR 0063 §1, §1a, §5).

`planchecks` decides; this module is what a hook calls: it builds the part's context from the branch,
asks the check, writes what §1 gives the hooks to write (a `push`, `conflict`, `rerequest`, `round`,
`draft` or `stop` entry), and holds the watch-only set of §1a.

Only hooks reach this module. It raises `planrows.Refusal`, which guard-github and guard-paths turn
into a blocked call, and never writes anything on a refusal except the `stop` the row names.

Standard library only: the hooks run Python with `-I -S` (ENG-3).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from trimtab import instance, planchecks, planledger, planreview, planrows, planscope, roots
from trimtab.guardstate import config_dir
from trimtab.planrows import FactsError, Refusal

ZERO = "0" * 40
BLOCKED_LABEL = "plan:blocked"
CONTINUE_LABEL = "plan:continue"
# §1a: a ledgerless session after ready may answer a thread only with one of these, since the other
# two tags each need a push (a commit for fixed, a register entry for a deferral).
WATCH_TAGS = ("wrong", "intentional", "moot")
PART = re.compile(r"plan/(?P<plan>[^/]+)/item-(?P<item>[0-9]{2})-part(?P<part>[0-9]+)$")


def now() -> str:
    """An RFC 3339 time with its offset, which every ledger line carries (TST-4)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def part_of(branch: str) -> tuple[str, int, int] | None:
    """The plan, item and part a `plan/<plan>/item-NN-partK` branch names, or None."""
    found = planscope.plan_of(f"{planscope.HEADS}{branch}")
    if found.ref is not planscope.Ref.PLAN:
        return None
    m = PART.fullmatch(branch)
    return (found.plan, int(m["item"]), int(m["part"])) if m else None


def limits_for(repo: Path, facts, branch: str, inst, item: int) -> planchecks.Limits | None:
    """E18's limits: the instance's `fix_limits`, and the lines the plan's item section names, read from
    the authorised plan at the merge-base — the tree an admin merged, never the working copy."""
    if inst is None or inst.fix_limits is None:
        return None  # E18 is not shown, so no push is allowed
    named = part_of(branch)
    if named is None:
        return None
    path = planscope.plan_files(named[0])[0]
    try:
        text = planscope.read_files(str(repo), facts.merge_base(branch), [path]).get(path)
    except planscope.PlanScopeError as err:
        raise FactsError(f"the authorised plan could not be read at the merge-base ({err})") from err
    if text is None:
        raise FactsError("the authorised plan is not at the merge-base, so E18's exception cannot be read")
    return planchecks.limits_of(inst.fix_limits, text, item)


def load_instance(instance_root: Path | None = None):
    """The instance file, or None where there is none: every row that needs a key then refuses."""
    try:
        return instance.load(roots.instance_root(instance_root))
    except (roots.InstanceError, instance.InstanceFileError):
        return None


def context(repo: Path, facts, branch: str, *, instance_root: Path | None = None,
            config: Path | None = None, inst=None, limits: bool = True) -> planchecks.Context:
    """One part's context. A branch that is not a part, or an instance with no review key, is a failed
    read: every row that rests on it refuses (SEC-2.5)."""
    named = part_of(branch)
    if named is None:
        raise FactsError(f"{branch!r} is not plan/<plan>/item-NN-partK, so no part's rows can be judged")
    plan, item, part = named
    found = inst if inst is not None else load_instance(instance_root)
    if found is None or found.review is None:
        raise FactsError("the instance names no review key, so the review rows are not shown")
    ledger = planledger.Ledger(config or config_dir(), facts.repository(), plan, item, part, clock=now)
    return planchecks.Context(repo=Path(repo), repository=facts.repository(), branch=branch, plan=plan,
                              item=item, part=part, facts=facts, ledger=ledger, agent=found.agent,
                              review_login=found.review.login, tags=dict(found.review.tags),
                              rerequest_label=found.review.rerequest_label,
                              limits=limits_for(Path(repo), facts, branch, found, item) if limits else None)


def _ctx(support, branch: str, **kw) -> planchecks.Context:
    return context(support.cwd, support.facts, branch, **kw)


def _refuse(verdict: planchecks.Verdict, ctx: planchecks.Context) -> None:
    """Record what the verdict names, then refuse or report the failed read as a refusal."""
    if verdict.conflicted_head:
        ctx.ledger.conflict(verdict.conflicted_head)
    if verdict.stop_reason:
        ctx.ledger.stop(verdict.stop_reason)
    if verdict.unreadable is not None:
        raise Refusal(f"a fact the plan rows need is unreadable ({verdict.unreadable}); refusing "
                      "(ADR 0063 §5)")
    if verdict.refusal is not None:
        raise Refusal(verdict.refusal)


def watch_only(ctx: planchecks.Context) -> bool:
    """§1a: the part's PR is open and not a draft, and this machine holds no ledger for the part."""
    pr = ctx.facts.open_pr(ctx.branch)
    return pr is not None and not pr.draft and not ctx.ledger.exists()


def _with_ledger(ctx: planchecks.Context, what: str) -> None:
    """Every act below needs the part's ledger. Without one the part is watch-only (§1a) or stops (§1)."""
    if ctx.ledger.exists():
        return
    if watch_only(ctx):
        raise Refusal(f"§1a: this session holds no ledger for the part, so {what} is refused; it is "
                      "watch-only until the operator restarts the part (ADR 0063 §1a)")
    raise Refusal(f"§1: this session holds no ledger for the part, so {what} is refused and the part "
                  "stops (ADR 0063 §1)")


# ---- the acts ----------------------------------------------------------------------------------

def ready_mark(support, branch: str, **kw) -> None:
    """The ready mark (`gh pr ready`, its REST spelling, GraphQL `markPullRequestReadyForReview`)."""
    if not planrows.plan_branch(branch):
        return
    try:
        ctx = _ctx(support, branch, limits=False, **kw)
    except FactsError as err:
        raise Refusal(f"a plan/* PR cannot be marked ready ({err}); refusing (ADR 0063 §5)") from err
    _with_ledger(ctx, "the ready mark")
    head = _head(ctx)
    _refuse(planchecks.ready(ctx, head), ctx)


def undraft(support, branch: str, **kw) -> None:
    """Returning the PR to draft. §1a refuses it without the ledger; with one it is the run's own act."""
    if not planrows.plan_branch(branch):
        return
    try:
        ctx = _ctx(support, branch, limits=False, **kw)
    except FactsError as err:
        raise Refusal(f"a plan/* PR cannot be returned to draft ({err}); refusing (ADR 0063 §1a)") from err
    _with_ledger(ctx, "returning the PR to draft")


def label_add(support, branch: str, label: str, **kw) -> None:
    """Adding a label. The instance's re-request label goes through E6 and E10; §1a allows only
    `plan:blocked`."""
    if not planrows.plan_branch(branch):
        return
    try:
        ctx = _ctx(support, branch, limits=False, **kw)
    except FactsError as err:
        raise Refusal(f"a label on a plan/* PR cannot be judged ({err}); refusing (ADR 0063 §5)") from err
    if label.casefold() != _rerequest_label(ctx).casefold():
        if label.casefold() == BLOCKED_LABEL or ctx.ledger.exists():
            return  # §1a allows plan:blocked; with the ledger, an ordinary label is no row's business
        _with_ledger(ctx, f"adding {label!r}")
        return
    _with_ledger(ctx, "a re-request")
    head = _head(ctx)
    verdict = planchecks.rerequest(ctx, head)
    _refuse(verdict, ctx)
    ctx.ledger.rerequest(head, "the run asked for a new CI review")


def label_remove(support, branch: str, label: str, **kw) -> None:
    """Removing a label. §1a allows removing `plan:continue` only."""
    if not planrows.plan_branch(branch) or label.casefold() == CONTINUE_LABEL:
        return
    try:
        ctx = _ctx(support, branch, limits=False, **kw)
    except FactsError as err:
        raise Refusal(f"a label on a plan/* PR cannot be judged ({err}); refusing (ADR 0063 §5)") from err
    _with_ledger(ctx, f"removing {label!r}")


def reply(support, branch: str, body: str, **kw) -> None:
    """A reply to a review comment. §1a allows the wrong, intentional and moot tags only: a fixed reply
    needs a commit (E9) and a deferral needs a register entry (E7), and both need a push."""
    if not planrows.plan_branch(branch):
        return
    try:
        ctx = _ctx(support, branch, limits=False, **kw)
    except FactsError as err:
        raise Refusal(f"a reply on a plan/* PR cannot be judged ({err}); refusing (ADR 0063 §1a)") from err
    if ctx.ledger.exists():
        return
    first = planreview.first_word(body)
    allowed = {ctx.tags[name] for name in WATCH_TAGS if name in ctx.tags}
    if first in allowed:
        return  # watch-only may answer a thread this way
    _with_ledger(ctx, "a reply opening with the fixed or deferral tag" if first else "an untagged reply")


def pushed(support, branch: str, source: str | None, **kw) -> None:
    """A `plan/*` push guard-github has already judged for E14-E17, E19 and E12's server count: E18 and
    E12's ledger conflicts, then the ledger's own record of the push (E13, E14)."""
    if not planrows.plan_branch(branch):
        return
    try:
        ctx = _ctx(support, branch, **kw)
    except FactsError as err:
        raise Refusal(f"a plan/* push cannot be judged ({err}); refusing (ADR 0063 §5)") from err
    old = ctx.facts.branch_head(branch) or ZERO
    _start(ctx, old)
    new = _source_sha(ctx, source)
    _refuse(planchecks.push(ctx, old=old, new=new, source=source), ctx)
    ctx.ledger.push(branch, old, new, run_env="1" if support.plan_run else "")


def round_recorded(support, branch: str, head: str, pass_name: str, *, before_draft: bool, **kw) -> None:
    """E11: one review pass completed on `head`. A sixth pre-draft round refuses (E23)."""
    if not planrows.plan_branch(branch):
        return
    ctx = _ctx(support, branch, limits=False, **kw)
    _start(ctx, ctx.facts.branch_head(branch) or ZERO)
    if before_draft:
        _refuse(planchecks.round_start(ctx, head), ctx)
    ctx.ledger.round(head, pass_name, before_draft=before_draft)


def draft_opened(support, branch: str, head: str, **kw) -> None:
    """E23: opening the draft PR needs a complete round on this exact head, and at most five pre-draft
    rounds. The ledger's `draft` entry is written only once the PR is allowed."""
    if not planrows.plan_branch(branch):
        return
    try:
        ctx = _ctx(support, branch, limits=False, **kw)
    except FactsError as err:
        raise Refusal(f"a plan/* PR cannot be opened ({err}); refusing (ADR 0063 §5)") from err
    _with_ledger(ctx, "opening the PR")
    _refuse(planchecks.draft(ctx, head), ctx)


# ---- the pieces each act needs -----------------------------------------------------------------

def _head(ctx: planchecks.Context) -> str:
    head = ctx.facts.branch_head(ctx.branch)
    if head is None:
        raise Refusal("the branch is not on the remote, so the plan rows have no head to judge "
                      "(ADR 0063 §5)")
    return head


def _rerequest_label(ctx: planchecks.Context) -> str:
    label = ctx.rerequest_label
    if not label:
        raise Refusal("the instance names no re-request label, so a label on a plan/* PR cannot be "
                      "judged (ADR 0063 §4)")
    return label


def _start(ctx: planchecks.Context, remote_head: str) -> None:
    """Start the part's ledger where it may be started: never for a part whose branch is already on the
    remote, since a fresh ledger would vouch for pushes it never saw (§1)."""
    try:
        ctx.ledger.start(remote_branch_exists=remote_head != ZERO)
    except planledger.LedgerError as err:
        raise Refusal(f"the part has no ledger and none may be started ({err}); refusing (ADR 0063 §1)") from err


def _source_sha(ctx: planchecks.Context, source: str | None) -> str:
    """The commit a push sends, as a SHA. A push whose source this guard cannot resolve is refused: the
    ledger could not record it, and E13 would then read the branch as holding work no run pushed."""
    if not source:
        raise Refusal("a plan/* write with no source commit cannot be recorded in the ledger, so E13 "
                      "could never resume the branch; refusing (ADR 0063 §1, §5)")
    try:
        return ctx.facts.commit_of(source)
    except FactsError as err:
        raise Refusal(f"the commit a plan/* push sends cannot be read ({err}); refusing "
                      "(ADR 0063 §1)") from err
