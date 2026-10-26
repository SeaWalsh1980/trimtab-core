# 0047. The plan-scope workflow drops the edited trigger

## Status

Accepted on merge. Changes the triggers chosen in trimtab-core#112 (ADR
0042, `docs/adr/0042-plan-scope-check-judges-the-pr-diff.md`, which is not
edited). A security control (SEC-7). Trimtab drops `edited` the same way in
Trimtab#167 (Trimtab ADR 0019).

## Context

`plan-scope.yml` ran on `edited` so that a plan PR retargeted onto the
default branch would be re-judged without a push. Since ADR 0044 the same
workflow also runs `merge-authority` for every PR, so an `edited` event now
costs two jobs on any PR, not only on plan PRs. A title or body edit,
which a plan run's checkpoint makes after each push, re-runs both to the
same result. `edited` also fires on a merged PR, where it re-judges the
merged head against the base as it is then and can stop the chain; the
workflow warned readers to leave a merged plan PR's title and body alone.

The operator re-runs a PR's checks by closing and reopening it, which fires
`reopened`.

## Decision

`plan-scope.yml` runs on `opened`, `synchronize` and `reopened` only. To
re-judge a PR without a push, close and reopen it.

## Options considered

| Option | Why it lost |
|---|---|
| Keep `edited` | Two billed jobs per title or body edit in private repositories, for the same result, and a merged plan PR re-judged by a later edit |
| Keep `edited` and skip merged PRs in merge-authority's `if:` | Stops only the merged-PR cost; an open PR's edits still re-run both jobs, and plan-scope's merged-PR re-judging stays |
| Keep `edited` but filter to base changes (`github.event.changes.base`) | A job-level condition still adds a run record, and a skipped plan-scope run on the head voids it (ADR 0043); the trigger cannot be filtered on what changed |

## Consequences

- A title or body edit starts no run, and a merged plan PR is never
  re-judged by an edit. The old warning to leave its title and body alone
  is gone.
- Retargeting a PR starts no run either. A plan PR moved onto the default
  branch keeps plan-scope runs judged against the old base, which failed
  (plan-scope requires the default branch), and a failed run voids the head
  until a push or a reopen. A PR moved off the default branch leaves its
  ruleset. Neither can pass by being retargeted.
- A `merge-authority` pass judged against another base and carried onto the
  default branch by a retarget was always the admin's approval. The
  checker's result depends on the base ref in one place only: the
  approver's approval counts only when the PR targets the default branch
  (ADR 0043). Against any other base, a non-plan PR needs the admin
  anyway, and a plan PR also fails plan-scope, which voids its head. So no
  approver approval, and no pass without the admin, survives a retarget. A
  re-run after the retarget reuses the old event's base, so the approver's
  approval still does not count until a push or a reopen starts a new
  event.
- `tests/trimtab/test_plan_scope_workflow.py` pins the three triggers.
