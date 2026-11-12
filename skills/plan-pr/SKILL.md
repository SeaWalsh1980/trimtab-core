---
name: plan-pr
description: Review and ready a plan item's PR — the one to three in-session review rounds, when to mark it ready or return it to draft, CI fix rounds, and who may merge it. Use before marking an item PR ready, before each later return to ready, and in a session started for an item PR's CI review; not for PRs outside an authorised plan, and not for opening the item's branch and record (plan-item).
---

# Review and ready a plan item's PR

Paths such as `plan-run/references/stops.md` are relative to the skills directory, the parent of
this skill's own directory.

**If you cannot read a file this skill links to, stop.** Make no file write unless this session
passed check 0 (plan-run/SKILL.md section 2), and never edit a merged record. If it passed and you
are working on an open item PR, set that PR's record's `## Status` to say whether this part finished
(`stopped unfinished` if not) and `## Next` to `blocked: <reason>`; a push to a PR marked ready
returns it to draft first, as plan-pr/SKILL.md says. Whether or not check 0 passed, label
`plan:blocked` and remove any `plan:continue` on the open item PR you are working on or, with none
open, on the merged PR that started the session; a valid restart with no open item PR labels
nothing. Report the blocker with one to three ways forward. Where a link below says more about an
unreadable file, that adds to this rule.

This is step 4 of plan-item/SKILL.md. Section numbers below are plan-run/SKILL.md's; other step
numbers are plan-item/SKILL.md's. Every stop follows plan-run/references/stops.md.

## In-session review rounds

**Review in session**, in one to three rounds before you mark the PR ready and again before each
later return to ready (trimtab-core ADRs 0060, 0062). A round runs `/trimtab-review`,
`/security-review` and `/code-review` on the PR's current head, in parallel, folded into one report;
a pass that fails or returns nothing usable is named in it, and the round is incomplete until it
runs. Grade each finding at the higher of the pass's own severity and the doctrine's
finding-severity rule: raise a pass's grade, never lower it. Record each finding outside the item's
scope as its own register entry in this PR (step 2), unless one in this PR already records it; one
graded CRITICAL or HIGH is also a stop (step 5), in any round.

- After round 1 or 2, fix every finding within scope, whatever its severity, push, and start the
  next round. The authorised plan covers these fixes: `/trimtab-review`'s "never auto-remediate"
  does not apply to them. A round with no finding within scope ends the review.
- Round 3's findings are not fixed in session: a CRITICAL or HIGH is a stop, and each MEDIUM or LOW
  becomes its own register entry.

## Ready

The rounds gate ready, not the draft. **Mark the PR ready** once the last round left no CRITICAL or
HIGH finding, in scope or out, every MEDIUM or LOW it left is a register entry, and the local checks
pass, through what the guards permit (`gh pr ready`, or `update_pull_request` with `draft` false
where `gh pr` is blocked). Ready describes one head: return the PR to draft before pushing anything
more (`gh pr ready --undo`, or `update_pull_request` with `draft` true where `gh pr` is blocked),
and mark it ready again only after reviewing the new head. The one exception is the commit that only
adds the last round's register entries and the progress record's links to them, touching nothing
outside the plan's register folder and this PR's progress record: it needs no further round. If a
guard refuses, stop as in step 5.

## CI fix rounds and the merge

Then fix CI review findings within scope, at most two CI fix rounds per PR; each fix goes back
through these rounds before ready. An infrastructure failure is re-run once and does not count. The
item PR needs an approving review from the approver or the admin on its final head commit, and is
merged by the admin, or by the approver when the conditions in section 3 hold. The agent account
never merges. Those conditions are in plan-run/references/record-authority.md.
