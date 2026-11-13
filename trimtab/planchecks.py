"""The four plan-run checks the hooks and `trimtab` subcommands call (ADR 0063 §5).

Each check joins the pure rows (`planreview`, `planrows`) to the facts (`planfacts`) and the part's
ledger (`planledger`), and reports one `Verdict`: allowed, refused with the row's own reason, or
unreadable. The application layer, so it holds no business rule of its own: every refusal comes from
a row's decision function, and every fact from a reader (ARC-3, ARC-4).

Nothing here writes the ledger. §1 gives that to the hooks, so a `Verdict` names what the caller must
append: the head a push answered while it conflicted with the default branch (E12 via §5a), and the
reason a part stops (E5, E12, E19). The `trimtab` subcommands report and write nothing at all.

Standard library only: the hooks run Python with `-I -S` (ENG-3).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from trimtab import planledger, planreview, planrows
from trimtab.planrows import Change, FactsError, Refusal


@dataclass(frozen=True)
class Limits:
    """E18's lists: the project's `fix_limits` and the lines the plan's item section names.

    `None` in place of a `Limits` is a refusal, never a skipped row: an instance with no `fix_limits`
    key shows no E18, so no run may push (ADR 0063 §5, Amendment 2's residual)."""

    markers: tuple[str, ...]
    thresholds: tuple[str, ...]
    named_lines: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Context:
    """One part's identity and the readers a check uses. `agent` is the instance's agent login; without
    one every row that names it refuses (ADR 0056)."""

    repo: Path
    repository: str
    branch: str
    plan: str
    item: int
    part: int
    facts: object  # a `planfacts.GitHubFacts`, or any reader with the same methods
    ledger: planledger.Ledger
    agent: str | None
    review_login: str
    tags: Mapping[str, str]
    limits: Limits | None = None
    rerequest_label: str = ""  # the instance's label, which a re-request is spelt with (§4)
    ledger_config: Path | None = None  # the config directory the part's ledger lives under (§1, §6)


@dataclass(frozen=True)
class Verdict:
    """What a check found. `ok` only when nothing refused and every fact was read."""

    refusal: str | None = None
    unreadable: str | None = None
    conflicted_head: str | None = None  # the hook writes a ledger `conflict` for it (§5a)
    stop_reason: str | None = None  # the hook writes a ledger `stop` with it (E5, E12, E19)

    @property
    def ok(self) -> bool:
        return self.refusal is None and self.unreadable is None


@dataclass
class _Notes:
    """What a check learns on the way, which the caller records whether or not a row then refuses."""

    conflicted: str | None = None
    stop: str | None = None


def _verdict(body, notes: _Notes) -> Verdict:
    """Run a check's body, turning a row's `Refusal` and any failed read into a `Verdict` (SEC-2.5)."""
    try:
        body()
    except Refusal as err:
        return Verdict(refusal=str(err), conflicted_head=notes.conflicted, stop_reason=notes.stop)
    except (FactsError, planledger.LedgerError) as err:
        return Verdict(unreadable=str(err), conflicted_head=notes.conflicted)
    return Verdict(conflicted_head=notes.conflicted, stop_reason=notes.stop)


def _agent(ctx: Context) -> str:
    if not ctx.agent:
        raise FactsError("the instance names no agent account, so the review rows cannot be judged")
    return ctx.agent


def _pr(ctx: Context) -> planrows.PrState:
    pr = ctx.facts.open_pr(ctx.branch)
    if pr is None:
        raise Refusal("the branch has no open PR, so the plan rows have nothing to judge (ADR 0063 §5)")
    return pr


def _ledger_pushes(ctx: Context, merge_base: str) -> list[planreview.LedgerPush]:
    """The ledger's pushes, oldest first, each with the commits it introduced (§5, Amendment 2)."""
    out = []
    for event in ctx.ledger.events():
        if event["type"] != "push":
            continue
        commits = ctx.facts.push_commits(event["old"], event["new"], merge_base)
        out.append(planreview.LedgerPush(event["old"], event["new"], event["run"], frozenset(commits)))
    return out


# ---- the ready mark: E5, E7, E8, E9, E19 -------------------------------------------------------

def ready(ctx: Context, head: str) -> Verdict:
    """`trimtab plan-ready-check`: the rows the ready mark must satisfy."""
    notes = _Notes()

    def body() -> None:
        agent = _agent(ctx)
        pr = _pr(ctx)
        runs = list(ctx.facts.facts_runs(ctx.branch, pr.number))
        reviews = [r for r, _ in runs]
        lines = [t for _, threads in runs for t in threads]
        # Every thread any run listed, each as its latest line has it: a thread an earlier run opened and
        # the latest run leaves out is still a thread that needs an answer (E8, E9).
        current = {t.id: t for _, threads in runs for t in threads}
        threads = [t for _, t in sorted(current.items())]
        comments = list(ctx.facts.review_comments(pr.number))

        try:
            planreview.check_e5(comments, lines, ctx.review_login)
        except Refusal:
            notes.stop = "E5: the review posted a comment its facts job did not list"
            raise
        planreview.check_e7(head, reviews, lambda h: _register_answer(ctx, h, head, threads, agent))

        merge_base = ctx.facts.merge_base(ctx.branch)
        base_tree = ctx.facts.tree_of(merge_base)
        # A review whose tree is the merge-base's ran on the default branch, not on this part's branch, so
        # its facts are read by commit as well as from the branch's own runs (E8).
        base_runs = [*runs, *ctx.facts.facts_runs_for(merge_base)]
        base_keys = frozenset(t.key for r, threads_of in base_runs if r.tree == base_tree
                              for t in threads_of)

        def deferral_ok(thread: planreview.Thread) -> bool:
            # The key is recomputed here at the merge-base and compared with the facts job's (E8).
            text = ctx.facts.line_text(merge_base, thread.path, thread.line)
            recomputed = None if text is None else planreview.thread_key(thread.path, thread.category, text)
            return planreview.deferral_allowed(
                thread, outside_diff=ctx.facts.outside_diff(ctx.branch, head, thread.path, thread.line),
                merge_base_keys=base_keys, recomputed_key=recomputed)

        planreview.check_e8(threads, comments, agent, dict(ctx.tags), deferral_ok)
        planreview.check_e9(threads, lines, comments, agent, dict(ctx.tags),
                            lambda t, sha: ctx.facts.fix_valid(ctx.branch, t, sha))

        scan = ctx.facts.secret_scan(head, ctx.branch)
        if scan != "pass":
            if scan != "pending":
                notes.stop = f"E19: the secret-scan job's result for the head is {scan}"
            raise Refusal(f"E19: the secret scan for this head is {scan}, so ready is refused "
                          "(ADR 0063 §5)")

    return _verdict(body, notes)


# ---- the re-request: E6, E10 -------------------------------------------------------------------

def rerequest(ctx: Context, head: str) -> Verdict:
    """`trimtab plan-rerequest-check`: the rows a re-request of the CI review must satisfy."""
    notes = _Notes()

    def body() -> None:
        pr = _pr(ctx)
        runs = list(ctx.facts.facts_runs(ctx.branch, pr.number))
        reran = [e["head"] for e in ctx.ledger.events() if e["type"] == "rerequest"]
        planreview.check_e6(head, [r for r, _ in runs], reran)

        merge_base = ctx.facts.merge_base(ctx.branch)
        planreview.check_e10(list(ctx.facts.branch_commits(ctx.branch)), ctx.ledger.complete_rounds(),
                             ctx.facts.last_answer(ctx.branch), _ledger_pushes(ctx, merge_base))

    return _verdict(body, notes)


# ---- a plan/* push: E12 with the ledger's conflicts, and E18 -----------------------------------

def push(ctx: Context, *, old: str, new: str, source: str | None = None) -> Verdict:
    """`trimtab plan-push-check`: E18 on the push's own change, and E12 counting the ledger's conflicts.

    guard-github's own reading (`planrows.judge_write`) still judges E14-E17, E19 and E12's server-side
    count for every `plan/*` write; this check adds the two rows that need the plan and the ledger."""
    notes = _Notes()

    def body() -> None:
        pr = ctx.facts.open_pr(ctx.branch)
        remote = ctx.facts.branch_head(ctx.branch)
        if ctx.limits is None:
            raise Refusal("E18: the instance names no fix_limits, so the row is not shown and no push is "
                          "allowed (ADR 0063 §5)")
        sent = source or new
        if remote is None:
            # The push creates the branch, so its own change is its diff from the merge-base with the
            # default branch. E18 judges it; E12 counts nothing, since there is no head it answers.
            planreview.check_e18(ctx.facts.change_from(ctx.facts.merge_base_of(sent), sent),
                                 ctx.limits.markers, ctx.limits.thresholds, ctx.limits.named_lines)
            return
        if pr is not None and ctx.facts.conflicted(pr.number):
            notes.conflicted = remote  # §5a: the head this push answers conflicted with the default branch
        changes = _own_change(ctx, remote, sent)
        planreview.check_e18(changes, ctx.limits.markers, ctx.limits.thresholds, ctx.limits.named_lines)

        conflicts = {e["head"] for e in ctx.ledger.events() if e["type"] == "conflict"}
        if notes.conflicted:
            conflicts.add(notes.conflicted)
        try:
            # E7's register-commit shape is judged on the push's plain diff from the remote head, never on
            # §5a's narrowed change: a merge of the default branch is no register commit.
            planrows.check_e12(ctx.branch, remote, ctx.facts, pr,
                               lambda: planrows.register_shape(ctx.facts.changes(ctx.branch, sent, remote),
                                                               ctx.branch),
                               conflicted_heads=frozenset(conflicts))
        except Refusal:
            notes.stop = "E12: the part has used its CI-fix rounds"
            raise

    return _verdict(body, notes)


def _register_answer(ctx: Context, reviewed: str, head: str, threads: Sequence[planreview.Thread],
                     agent: str) -> bool:
    """E7's alternative: the head differs from a reviewed one only by the register-commit shape, **and**
    every register file it adds is named in an E3-listed thread's deferral reply.

    Without that second clause a run could commit any content as a register entry and have ready accept
    a head no review ever saw (ADR 0063 §5, E7)."""
    if not ctx.facts.register_answer(ctx.branch, reviewed, head):
        return False
    changes = ctx.facts.changes(ctx.branch, head, reviewed)
    added = [c.path for c in changes if c.status == "A" and planrows.register_file(c.path, ctx.branch)]
    if not added:
        return False  # the shape holds only for a push that adds register entries
    deferral = ctx.tags.get("deferred", "")
    listed = {t.id for t in threads}
    replies = [c.body for c in ctx.facts.review_comments(ctx.facts.open_pr(ctx.branch).number)
               if c.user.casefold() == agent.casefold() and c.in_reply_to in listed
               and deferral and planreview.first_word(c.body) == deferral]
    return all(any(_names(body, path) for body in replies) for path in added)


def _names(body: str, path: str) -> bool:
    """Whether a reply names a path as a whole word, not as a piece of a longer one: `a/x.md` is not named
    by `b/a/x.md`, nor `item-02-part1-01.md` by `item-02-part1-01.md.bak`."""
    return re.search(rf"(?<![A-Za-z0-9_./-]){re.escape(path)}(?![A-Za-z0-9_/-])(?!\.[A-Za-z0-9])", body) is not None


def _own_change(ctx: Context, remote: str, sent: str) -> Sequence[Change]:
    """The push's own change (E18): its diff from the remote head, except for a merge of the default
    branch, where it is the merge commit's diff from the default-branch parent, so main's own changes are
    never judged as the run's (ADR 0063 §5a)."""
    parents = ctx.facts.parents(sent)
    if len(parents) > 1:
        from_default = [p for p in parents if ctx.facts.on_default(p)]
        if from_default:
            return ctx.facts.change_from(from_default[-1], sent)
    return ctx.facts.changes(ctx.branch, sent, remote)


# ---- resuming a branch: E13 --------------------------------------------------------------------

def resume(ctx: Context, head: str, *, scope_ok: bool) -> Verdict:
    """`trimtab plan-resume-check`: E13. `scope_ok` is `planscope`'s verdict on the diff from the
    merge-base, which the caller reads (the CLI through `planscope.check`)."""
    notes = _Notes()

    def body() -> None:
        agent = _agent(ctx)
        merge_base = ctx.facts.merge_base(ctx.branch)
        planreview.check_e13(list(ctx.facts.branch_commits(ctx.branch)), _ledger_pushes(ctx, merge_base),
                             list(ctx.facts.activity(ctx.branch)), head, merge_base, agent,
                             ctx.facts.on_default, scope_ok)

    return _verdict(body, notes)


# ---- opening the draft PR: E23, and a round starting: E23's sixth-round refusal ----------------

def draft(ctx: Context, head: str) -> Verdict:
    """E23: opening a PR from `plan/*` needs a complete round on the exact head, and at most five
    pre-draft rounds."""
    notes = _Notes()

    def body() -> None:
        planreview.check_e23(head, ctx.ledger.complete_rounds(), ctx.ledger.pre_draft_heads())

    return _verdict(body, notes)


def round_start(ctx: Context, head: str) -> Verdict:
    """E23's other half: a sixth pre-draft round refuses at its first pass (E11's PreToolUse)."""
    notes = _Notes()

    def body() -> None:
        planreview.check_round_start(head, ctx.ledger.pre_draft_heads())

    return _verdict(body, notes)


def limits_of(fix_limits, plan_text: str | None, item: int) -> Limits | None:
    """`Limits` from the instance's `fix_limits` and the plan's item section, or None where the instance
    names no `fix_limits` (E18 is then not shown, and `push` refuses)."""
    if fix_limits is None:
        return None
    named = planreview.named_lines(plan_text, item) if plan_text is not None else frozenset()
    return Limits(tuple(fix_limits.markers), tuple(fix_limits.thresholds), named)
