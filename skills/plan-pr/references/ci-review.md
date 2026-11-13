# The CI review and its threads

Paths such as `plan-run/SKILL.md` are relative to the skills directory, two levels above this file.

Steps 1 and 2 of plan-pr/SKILL.md. The facts and refusals named here are trimtab-core ADR 0063's
(§4 for the project's facts, §5 for the guards).

## Which review counts

The CI review is the review job the pinned base or the instance configuration names (the instance's
`review` key), never one a PR or a session names. Its facts come from the separate facts job's log,
never from the model's output: the `TRIMTAB-REVIEW v1` line gives the verdict (`pass`, `fail` or
`fail-closed`) and the head and tree it reviewed, and each `TRIMTAB-THREAD v1` line gives a thread's
root comment id, severity, state, path and line.

| A thread or verdict | Then |
|---|---|
| Listed by the facts job for this PR | Act on it (plan-pr/SKILL.md step 2). |
| Not listed, and from the review's login, once every review run on the PR has completed and the facts job of each run whose review job ran has finished | A stop: the guard also refuses ready while it stands (E5). A run still queued or in progress is waited for. A completed run whose review job was skipped or cancelled before its model step posts nothing and has no facts to wait for. Until the others' facts jobs finish, judge nothing; a facts job that does not finish within the review bound is the no-readable-verdict stop. |
| Not listed, from anyone else (a person, another bot) | Do not act on it. Name it for the operator in a PR comment. |

**A verdict holds for its head.** A CRITICAL or HIGH verdict stands until a new commit, whatever a
later review of the same head says. A thread counts as closed only when a later review's facts list
it as resolved, or the admin closes it, never because a finding is absent from a later review (E9).
Never resolve a thread yourself.

## Outcome tags

The project names its tags (the instance's `review` key: fixed, wrong, intentional, moot, deferred).
Each reply opens with one, as its first word, and then says what you did. Take the first row that
applies:

| The thread's finding | What you do | Reply opens with |
|---|---|---|
| CRITICAL or HIGH, within scope, and you fixed it | Fix, push, cite the commit | the fixed tag, then the commit's full SHA |
| CRITICAL or HIGH, anything else (out of scope, wrong, cannot fix) | A stop; out of scope, also a register entry | no tag: only the fixed tag is accepted on it |
| MEDIUM or LOW, within scope, fixed | Fix, push, cite the commit | the fixed tag, then the commit's full SHA |
| MEDIUM or LOW, within scope, and it cannot be fixed within the fix limits | A stop | no tag |
| MEDIUM or LOW, out of scope | A register entry (plan-item/references/records.md) | the deferral tag, then the entry's path |
| MEDIUM or LOW, the finding is not true of the code | Nothing | the wrong tag, with the reason |
| MEDIUM or LOW, the behaviour is deliberate and in the item's brief | Nothing | the intentional tag, citing the brief |
| MEDIUM or LOW, the code it names is gone from the head | Nothing | the moot tag, citing the commit that removed it |

The wrong, intentional and moot tags are your own claim: no guard checks them. Use one only when
it is true of the code, with its citation (the reason, the brief, the removing commit); never for a
finding you simply did not fix.

What the guard holds you to (E8, E9):

- **Fixed** cites, by full SHA, a commit on the branch after the thread's commit that changes at
  least one line in the thread's hunk.
- **Deferral** is accepted only below HIGH, only on a line outside the head side of the branch's
  diff from its merge-base, and only when a CI review of the merge-base's exact tree raised the
  same finding. Where the project has no such review, a deferral is refused, so every
  out-of-scope thread is a stop. Accepted as the strict reading (trimtab-core ADR 0063).
- A reply needing a push (fixed, deferral) on a ready PR returns it to draft first
  (plan-pr/SKILL.md step 4).
