---
name: plan-item
description: Work one item, or one part of an item, of an authorised plan — sizing the part to one session, the `plan/…` branch and its pushed checkpoints, the plan's write scope and fix limits, the progress record and register entries, up to five in-session review rounds, and opening the draft once they are done. Use once plan-run has gated the next work, or when you resume an item's branch; not for deciding what comes next (plan-run), or for CI review, ready and the watch to merge (plan-pr).
---

# Work a plan item

Paths such as `plan-run/references/stops.md` are relative to the skills directory, the parent of
this skill's own directory.

**If you cannot read a file this skill links to, that is a stop:** follow
plan-run/references/stops.md. If you cannot read that either, make no file write; in a plan run,
label `plan:blocked` and remove any `plan:continue` on the open item PR or, with none open, on the
merged PR whose progress record supplied the next work (the plan's own PR for item 01, or the
merged PR that started the session if you have not found that record yet); and report the
blocker, naming the file, with one to three ways forward. The record's `## Next` is not updated
then, so where it and the label disagree, the label is authoritative. Where a link below says more
about an unreadable file, that adds to this rule.

`<plans>` and `<plan>` are as plan-run/SKILL.md defines them. Section numbers in this file are
plan-run/SKILL.md's; step numbers are this file's. Every stop follows plan-run/references/stops.md.

## Working a plan item

One item, or one part of an item, per session, ending in one PR that this session owns to merge:

### 1. Size the part to one session

A part runs, in one session: the work, up to five in-session rounds, the CI review on the draft, up
to three CI fix rounds with one in-session round each, and the watch to merge. Before ready, its
state cannot move to another session except a later one on the same machine and config dir
(trimtab-core ADR 0063 §1b), so a part too big for its session costs a stop, never a weaker gate.

| When | Then |
|---|---|
| Starting a part the plan's item makes large | Take only as much of the item as one session can finish to ready. The rest of the item is part K+1. |
| Partway through, you expect not to reach ready in this session | End the part early at a clean point: the in-scope work done so far, a register entry for each piece of the item's scope left, `## Status` `item NN part K complete; item continues` and `## Next` naming part K+1 with its brief. Then go on to step 5 with what you have. |
| You cannot reach a clean point | Stop (step 8). |

### 2. Work on the branch, with no PR

Work on a branch named `plan/<plan>/item-NN-partK` from its first push, so a later session can see
it is in flight. Push a checkpoint at each milestone: if the session dies, the next one resumes from
the branch instead of starting over (plan-run/references/in-flight.md says when it may). Open no PR
yet: the draft opens only after the in-session rounds (step 6), because CI review starts when it
does. Push fast-forwards only: never rewrite, close or delete a `plan/*` ref.

### 3. Stay in scope, and inside the fix limits

Write only inside the plan's scope: the third condition in plan-run/references/gate.md. If you
cannot read it, the gate fails. Then:

| Route | Then |
|---|---|
| An edit tool's write that the guard refuses as outside the scope | Do not look for another route: record the change as a register entry, or stop (step 8) if the item needs it. |
| A Bash write that changed a path outside the scope (reported after the call) | Do not commit it. It is a stop: name the path in the `blocked:` reason. |
| A finding or check you are about to fix | Fix it only if it is within the item's scope, and only within the fix limits. Both are defined in plan-item/references/scope.md. |

Keep temporary files only in the session scratch directory: the one the session-scratch hook named
at session start, or the harness's scratchpad; the hook removes both when the session ends
(trimtab-core ADR 0049). Where neither exists, make one directory under the system temp directory
named after the session and remove it before the session ends. Name in `## Done` anything you leave
that the hook does not remove.

### 4. Keep the progress record and the register

Write the progress record and any register entries on the branch as you go, and before the draft
opens: plan-item/references/records.md. A register entry written before the PR exists names the
branch. Each in-session round's outcome goes into the record.

### 5. Review in session, before the draft

Run one to five in-session review rounds on the branch's current head, as
plan-item/references/rounds.md says. The rounds gate the draft.

### 6. Open the draft and label it

- **Open the PR as a draft** from the branch only once the last round ended the review, with the
  record and every entry pushed. Its head is the head the last round reviewed, plus at most the one
  commit that adds that round's outcome to the record and its register entries, touching nothing
  else. The guard refuses a draft unless its ledger records a complete round on the head, and at
  most five pre-draft rounds (E23); if it refuses, do not route around it: that is a stop.
- **Label** the PR `plan:continue` unless `## Next` is `plan complete` or `blocked: <reason>`, as
  soon as it opens. (When you author a PR that adds an authorised plan, label it `plan:continue`
  too: its merge starts item 01 part 1. Put it on a branch outside `plan/`: `plan/*` branches are
  for item PRs only, and any other `plan/…` branch has every write refused.)

### 7. Own the PR to merge

The session that opens the draft owns the PR until it merges or a stop applies: the CI review on
the draft, every thread, ready, and the watch to merge are plan-pr/SKILL.md.

### 8. Stop when

| Cause | Where it comes from |
|---|---|
| The work cannot be finished, or cannot reach a clean point (step 1) | this skill |
| It needs work outside its scope, a write a hook refuses, or the authorised plan changed | step 3 |
| A stray Bash write, or a secret-scan failure | step 3 and plan-item/references/scope.md |
| A CRITICAL or HIGH after the fifth in-session round, or out of scope in any round | step 5 |
| A stop named in the CI review, CI fix, ready or watch steps, or the CI fix-round limit reached | plan-pr/SKILL.md |
| You can already see the next work would fail the gate in section 4 | plan-run/SKILL.md |

Then stop as plan-run/references/stops.md says: before the draft opens, the label goes on the merged
PR whose record supplied this work; after, on this PR.

The session ends at a stop or when the watch bound runs out (plan-pr/SKILL.md). When the PR merges,
it continues with plan-run/SKILL.md from section 1, whose section 7 hands over to the next session.
