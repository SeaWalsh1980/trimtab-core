# Stops

Paths such as `plan-run/SKILL.md` are relative to the skills directory, two levels above this file.

Every stop in a plan run follows this, whichever skill it happens in (plan-run, plan-item, plan-pr).
Section numbers in this file are plan-run/SKILL.md's.

## Every stop

**Every stop** reports the blocker and proposes one to three ways forward for the operator (for
example: approve a new plan covering it, make the guarded change by hand, or correct the record in a
reviewed PR). Then, by where the part is; take the first row that applies:

| Where the part is | Record | Labels |
|---|---|---|
| The merge was not confirmed (section 1) | No file write. | None: that stop reports only. |
| No branch pushed yet: the authorisation check (section 2) or the gate (section 4) failed at the start of a session | No file write. | `plan:blocked` on the merged PR whose progress record the session took its next work from (the plan's own PR for item 01), through `gh api` if `gh pr` is blocked; remove `plan:continue` from it. |
| Work began on a `plan/…` branch, and its draft has not opened | Commit the progress record on the branch, with `## Status` saying whether this part finished (`stopped unfinished` if not) and `## Next` reading `blocked: <reason>`, and push it, unless the stop is one of the "Stops that forbid a write". | The same merged PR as the row above: `plan:blocked` on, `plan:continue` off. |
| The item's PR is open | As the row above, pushed to the PR's branch (returning the PR to draft first if it is ready). | `plan:blocked` on the PR; remove any `plan:continue` from it. The labels always follow the final `## Next`. |

A restart counts as the first row too: its record PR is the latest authorised record's. So every
stop but the unconfirmed merge leaves a label the admin can remove to allow a restart.

## Stops that forbid a write

- **Launched without `TRIMTAB_PLAN_RUN=1`** (check 0): no file write at all, not even the record.
  Label as the table says and report.
- **A secret-scan failure:** never fixed by the run. The only push allowed is one touching only the
  part's progress record.
- **No round ledger for the part** (plan-pr/SKILL.md, "Who owns the PR"): no file write or push,
  whether the PR is ready (watch-only) or a draft. Label the PR and report.
- **A stray Bash write** (a path the call changed outside the plan's scope, reported after the
  call): do not commit it, and name its path in the `blocked:` reason of `## Next`.

## When the label and the record disagree

The label is authoritative. A stop that could not write its record (an unreadable stops file, a
refused write, a forbidden one) leaves `## Next` still naming work while `plan:blocked` is on. A
session reads the label first: a part, PR or record PR carrying `plan:blocked` stays stopped until
a valid restart, whatever its `## Next` says.

## A finished PR that stopped

A PR whose own work is finished can still carry `plan:blocked`; it merges normally, and its merge
starts no session — say so rather than promising one.
