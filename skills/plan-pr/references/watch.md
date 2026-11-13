# The watch to merge

Paths such as `plan-run/SKILL.md` are relative to the skills directory, two levels above this file.

Step 5 of plan-pr/SKILL.md. Step numbers in this file are plan-pr/SKILL.md's.

## What each event means

Watch from ready until the PR merges, a stop applies, or the bound runs out (the project's
instruction file names it; 24 hours from ready where it names none). Read the PR's head checks
from what your token can read (Actions job records and logs), its review threads, and its
`mergeable` state. Take the first row that applies:

| Event | Then |
|---|---|
| The PR merged | Continue with plan-run/SKILL.md from section 1. This session did the merged work, so its section 7 hands over. |
| An infrastructure failure not yet re-run on this head: a job that never reached its steps, or one its log shows failed on a runner, network or service error before running the project's own command | Re-run it once; it does not count as a round. A job already re-run once on this head (its run shows an attempt above 1) skips this row: take it as a failing check (the rows below). |
| A failing check within the item's scope (it passes on the merge-base; plan-item/references/scope.md; an unreadable merge-base result is a stop) | Return the PR to draft, then fix it as a CI fix round (step 3). Mark it ready again only as step 4 says. |
| A failing check outside the item's scope | Name it for the operator in a PR comment. A stop; never fix it. |
| A check you cannot read, or a re-run your token may not trigger | Name it for the operator in a PR comment, and in the progress record with the next push if one is made. Never treat it as passed. Keep watching. |
| A new CI thread | Handle it as step 2. If the reply needs a commit, return the PR to draft first. |
| `mergeable` is `false`: a conflict with the default branch | Resolve it as below. |
| `mergeable` is `null` (GitHub still computing) | Read it again after a minute, up to ten times; still `null` after that, name it for the operator in a PR comment and keep watching. |
| The approver or the admin requests changes, or an approval is dismissed | Name it for the operator in a PR comment. A stop: the run acts only on CI threads. |
| The bound runs out, the PR unmerged and no stop | Report it in a PR comment and in the session's final output. Not a stop: the session ends, and the PR waits for a session started for it. |

## A conflict with the default branch

The approver refuses a head that conflicts with the default branch, and resolving it is the run's
job (trimtab-core ADR 0063 §5a):

1. Return the PR to draft.
2. Merge the default branch into the `plan/…` branch (a merge commit on top, never a rebase),
   resolve the conflict within the item's scope, and push.
3. That push is a CI fix round, counted against the same cap of three, and needs its one
   in-session round on the merged head before the re-request (step 3).

Scope is judged from the new merge-base after the merge, so the default branch's changes are not
the item's. A conflict you cannot resolve within the item's scope is a stop, and so is one after the
third CI fix round.

## Watch-only

A session that holds no round ledger for the part, with the PR ready, may only watch
(trimtab-core ADR 0063 §1a). The guards allow it:

- reads;
- PR comments;
- replies opening with the wrong, intentional or moot tag;
- adding `plan:blocked` and removing `plan:continue`.

They refuse every file write, push and ref write, a return to draft, a re-request, and a reply with
the fixed or deferral tag, since both need a push. So in watch-only, a failing check, a thread that
needs a fix or a deferral, or a conflict is a stop: label as plan-run/references/stops.md says for
an open PR, with no record written. If you cannot read it, label the PR `plan:blocked` and remove
`plan:continue` anyway.
