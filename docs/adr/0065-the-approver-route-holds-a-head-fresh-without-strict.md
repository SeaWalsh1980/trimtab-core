# 0065. The approver route holds a head fresh without "require branches to be up to date"

## Status

Accepted on merge. Amends ADR 0057's containment: the route no longer asks for the
ruleset to hold `plan-scope` to a strict policy, and asks instead that the head be fresh
against the default branch, judged from git. 0057 is not edited, and the rest of it stands.

## Context

plan-scope judges a plan PR against the base commit of the event that started it, and
nothing re-runs it when the base branch moves (see the comment in `plan-scope.yml`). So if
the admin narrows the write allowlist or withdraws the plan after the last push, the old
pass still stands. Until now, the ruleset's "require branches to be up to date" (strict)
closed that gap: the PR had to take the new base, and that push re-ran plan-scope.
`containment_problems` refused the approver route on any ruleset whose plan-scope rule
was not strict.

On 2026-11-12 the owner turned strict off on ControlTower's `main`. With strict on, every
merge forced every open PR to update and re-run CI. The owner decided that:

- the approver refuses only a head that conflicts with `main`;
- the plan run resolves a conflict by merging `main` into the head, and that push counts
  as a CI fix round.

At ControlTower's locked base (4b91db2d), strict off closes the approver route for every
approve and merge, in both `check-merge-authority` and `check-approver-route`. Rows may
only tighten (NRM-3), so the strict requirement could not just be dropped. Something had
to keep plan-scope's pass judged against what `main` now holds (SEC-2.3 "Fresh").

No measurement drove this. The cost of strict is a cost in process: the forced updates
and the CI runs they trigger.

## Decision

`route_problems` no longer reads strict. Each time it reads the route, it reads the head's
freshness from git, over the base and head commits it is given. For
`check-approver-route` that base is the default branch's tip in the checkout, and the head
is the commit the write will be pinned to. Freshness refuses the route, naming the cause,
when:

1. the base and the head do not have exactly one merge-base (`git merge-base --all`);
2. `git merge-tree --write-tree` reports a conflict (exit 1 with a tree id). Any other
   answer is an error;
3. main's first-parent history since the merge-base (`git log --first-parent
   --diff-merges=first-parent --no-renames`, so a merge's changes and a revert still show)
   touches a file plan-scope's verdict depends on. Those files are taken from the code's
   own constants:
   - the write allowlist (`planscope.ALLOWLIST`);
   - the PR's plan and its `.writes`, through `planscope.plan_of` and
     `planscope.plan_files`;
   - the pin files (`.claude/trimtab.json`, `instance.json`);
   - the plan-scope workflow, both the expected path and the one configured with
     `--job-workflow`;
4. that history touches a path the head adds, where the create-only check reads the base;
5. that history deletes, or renames away, a path the head changes. merge-tree detects
   renames even with `diff.renames` off, so it would carry the head's edit onto a path
   plan-scope never judged;
6. that history touches an instruction file (`CLAUDE.md`, `CLAUDE.local.md`, in any
   directory) or the rules directory the base commit's lock names (by default
   `.claude/rules/`). Strict kept the verdict of a merge judged against those current too.

Freshness is only as current as the base it is given. So `check-approver-route` also reads
the default branch's tip from the server, last among its route reads, and refuses when
`--base` is not that commit. Strict was a server-side control, and this keeps the base a
server fact rather than whatever the caller's checkout held. `check-merge-authority` judges
against the base branch's tip as its job starts. Its pass can go stale as `main` moves, but
the approver App never writes on that pass alone: it runs `check-approver-route` first.

The merge-base is never newer than the base plan-scope judged. The event's base was
`main`'s tip at the push, and the head merged nothing from `main` after it. So reading
from the merge-base can hold too much, never too little.

A strict ruleset with an up-to-date head still passes. Its merge-base is the base itself,
so there is nothing to read. Every read is bounded:

- git's 30 s timeout per call;
- at most 20,000 path entries from main's history, past which the read is an error.

Any unreadable answer is an error, which vouches for nothing (SEC-2.5). merge-tree writes
the merge's objects to a scratch object directory that is deleted at once, so judging
writes nothing to the repository (SEC-2.3). The `RouteFacts.freshness` field defaults to
unread, and unread closes the route.

## Options considered

| Option | Why it lost |
|---|---|
| Keep requiring strict | The owner turned it off: every merge forced every PR to update and re-run CI |
| Delete the strict check | Rows only tighten (NRM-3); a stale plan-scope pass would then count against a narrowed allowlist or a withdrawn plan |
| A consumer-side list of inputs to hold on | A hand-kept copy of what plan-scope reads drifts from the code; core reads its own constants, and the consumer's preflight cannot vouch for core's route |
| Re-run plan-scope against the new base | Needs a workflow trigger on base moves (a SEC-7 control change), and a check run cannot vouch for its own freshness at the moment the App writes |
| Refuse on any change on `main` since the merge-base | That is strict again, enforced by core |

## Consequences

- A lock bump on `main` holds every open plan PR until it merges `main`. That holds too
  much: most bumps don't change plan-scope's verdict. Accepted, because the checker the
  pin selects is the verdict.
- A clean merge can still clash in meaning: `main` changes code the head's change relies
  on, and no file plan-scope reads moved. The owner accepts this risk. CI on the merged
  result is not re-run by this check.
- Strict also kept every other required check, `lint-test` among them, judged against the
  current base. That is lost for the approver route too, and is part of the same accepted
  risk.
- `main` can still move between `check-approver-route`'s tip read and the App's merge call:
  GitHub's merge API pins the head, not the base. Strict closed that window on GitHub's
  side. SEC-2.3 leaves an unpinned write to the merge's own pin, and the window is part of
  the owner's accepted risk.
- A conflict, or any hold here, sends the plan run to merge `main`, and that push counts
  as a CI fix round.
- The strict comment in `.github/workflows/plan-scope.yml` now describes only rulesets
  that still use strict. It is a SEC-7 control, so it changes only in a PR of its own
  with the operator's authorisation.
- Consumers pick this up only when they move their lock.
